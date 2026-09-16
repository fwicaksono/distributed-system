#!/usr/bin/env bash
# Security Groups: one per role, rules reference OTHER SECURITY GROUPS instead of IPs.
#
# "Allow 5432 from sg-app" means "from any instance that carries sg-app", now and in the
# future. Add a second app node and it automatically gets database access; nothing else does.
#
# Security Groups are STATEFUL: allowing inbound 443 automatically allows the response
# packets out. Outbound is left at the default allow-all (needed for apt, ECR, SSM).
source "$(dirname "$0")/00-config.sh"
VPC_ID=$(load VPC_ID)

sg() {  # sg <key> <name> <description>
  local id
  id=$(aws ec2 create-security-group --region "$AWS_REGION" --vpc-id "$VPC_ID" \
    --group-name "$PROJECT-$2" --description "$3" \
    --tag-specifications "$(tags security-group "$2")" --query GroupId --output text)
  save "$1" "$id"
  echo "$id"
}
# JSON --ip-permissions (not shorthand): descriptions may contain spaces and commas.
# Allowed description characters: a-z A-Z 0-9 spaces ._-:/()#,@[]+=&;{}!$*
from_cidr() {  # from_cidr <sg> <port> <cidr> <description>
  aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$1" \
    --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":$2,\"ToPort\":$2,\"IpRanges\":[{\"CidrIp\":\"$3\",\"Description\":\"$4\"}]}]" >/dev/null
}
from_sg() {  # from_sg <sg> <port-from> <port-to> <source-sg> <description>
  aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$1" \
    --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":$2,\"ToPort\":$3,\"UserIdGroupPairs\":[{\"GroupId\":\"$4\",\"Description\":\"$5\"}]}]" >/dev/null
}

log "Creating security groups"
SG_EDGE=$(sg SG_EDGE edge "Public entry point: Nginx 80/443, SSH from admin IP")
SG_APP=$(sg SG_APP app "API servers")
SG_BROKER=$(sg SG_BROKER broker "RabbitMQ + worker")
SG_OPS=$(sg SG_OPS ops "Prometheus, Grafana, worker")
SG_DATA=$(sg SG_DATA data "PostgreSQL + Redis")
SG_NAT=$(sg SG_NAT nat "NAT instance for private subnets")

log "sg-edge"
from_cidr "$SG_EDGE" 80  0.0.0.0/0 "HTTP: ACME challenge + redirect to HTTPS"
from_cidr "$SG_EDGE" 443 0.0.0.0/0 "HTTPS from the internet"
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$SG_EDGE" \
  --ip-permissions '[{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"Ipv6Ranges":[{"CidrIpv6":"::/0","Description":"HTTPS over IPv6"}]}]' >/dev/null
from_cidr "$SG_EDGE" 22 "$MY_IP/32" "SSH from admin IP only"
from_sg "$SG_EDGE" 8000 8000 "$SG_OPS" "Prometheus scrapes api-1"
from_sg "$SG_EDGE" 9100 9100 "$SG_OPS" "Prometheus scrapes node-exporter"

log "sg-app"
from_sg "$SG_APP" 8000 8001 "$SG_EDGE" "Nginx to api-2 and api-3"
from_sg "$SG_APP" 8000 8001 "$SG_OPS"  "Prometheus scrapes APIs"
from_sg "$SG_APP" 9100 9100 "$SG_OPS"  "node-exporter"
from_sg "$SG_APP" 22 22 "$SG_EDGE"     "SSH via edge bastion"

log "sg-broker"
for src in "$SG_EDGE" "$SG_APP" "$SG_OPS"; do
  from_sg "$SG_BROKER" 5672 5672 "$src" "AMQP from producers/consumers"
done
from_sg "$SG_BROKER" 15692 15692 "$SG_OPS" "RabbitMQ Prometheus metrics"
from_sg "$SG_BROKER" 9101 9101 "$SG_OPS"   "worker-1 metrics"
from_sg "$SG_BROKER" 9100 9100 "$SG_OPS"   "node-exporter"
from_sg "$SG_BROKER" 22 22 "$SG_EDGE"      "SSH via edge bastion"

log "sg-ops"
from_sg "$SG_OPS" 9100 9100 "$SG_OPS" "node-exporter (self)"
from_sg "$SG_OPS" 22 22 "$SG_EDGE"    "SSH via edge bastion"

log "sg-data"
for src in "$SG_EDGE" "$SG_APP" "$SG_BROKER" "$SG_OPS"; do
  from_sg "$SG_DATA" 5432 5432 "$src" "PostgreSQL from app tier"
  from_sg "$SG_DATA" 6379 6379 "$src" "Redis from app tier"
done
from_sg "$SG_DATA" 9187 9187 "$SG_OPS" "postgres-exporter"
from_sg "$SG_DATA" 9121 9121 "$SG_OPS" "redis-exporter"
from_sg "$SG_DATA" 9100 9100 "$SG_OPS" "node-exporter"
from_sg "$SG_DATA" 22 22 "$SG_EDGE"    "SSH via edge bastion"

log "sg-nat: forwards traffic that originates inside the VPC"
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$SG_NAT" \
  --ip-permissions "[{\"IpProtocol\":\"-1\",\"IpRanges\":[{\"CidrIp\":\"$VPC_CIDR\",\"Description\":\"All traffic from the VPC to be NATed\"}]}]" >/dev/null

log "Done"
aws ec2 describe-security-groups --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[].[GroupName,GroupId,length(IpPermissions)]' --output table
