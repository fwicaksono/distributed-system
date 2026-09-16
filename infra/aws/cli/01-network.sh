#!/usr/bin/env bash
# VPC, subnets, Internet Gateway, route tables.  (NAT instance routing is added by 04.)
#
#   Internet
#      │
#   [Internet Gateway]  ── attached to VPC 10.20.0.0/16
#      │
#   public route table:  0.0.0.0/0 -> IGW          private route table: 0.0.0.0/0 -> NAT instance
#      │                                                 │
#   public subnets 10.20.1.0/24, 10.20.2.0/24       private subnets 10.20.11.0/24, 10.20.12.0/24
#
# A subnet is "public" ONLY because its route table sends 0.0.0.0/0 to an Internet Gateway.
source "$(dirname "$0")/00-config.sh"

log "VPC $VPC_CIDR"
VPC_ID=$(aws ec2 create-vpc --region "$AWS_REGION" --cidr-block "$VPC_CIDR" \
  --tag-specifications "$(tags vpc vpc)" --query Vpc.VpcId --output text)
aws ec2 wait vpc-available --region "$AWS_REGION" --vpc-ids "$VPC_ID"
# DNS support + hostnames: instances get resolvable names; required by VPC endpoints later.
aws ec2 modify-vpc-attribute --region "$AWS_REGION" --vpc-id "$VPC_ID" --enable-dns-support
aws ec2 modify-vpc-attribute --region "$AWS_REGION" --vpc-id "$VPC_ID" --enable-dns-hostnames
save VPC_ID "$VPC_ID"

log "Internet Gateway"
IGW_ID=$(aws ec2 create-internet-gateway --region "$AWS_REGION" \
  --tag-specifications "$(tags internet-gateway igw)" --query InternetGateway.InternetGatewayId --output text)
aws ec2 attach-internet-gateway --region "$AWS_REGION" --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
save IGW_ID "$IGW_ID"

subnet() {  # subnet <key> <cidr> <az> <name>
  local id
  id=$(aws ec2 create-subnet --region "$AWS_REGION" --vpc-id "$VPC_ID" --cidr-block "$2" \
    --availability-zone "$3" --tag-specifications "$(tags subnet "$4")" \
    --query Subnet.SubnetId --output text)
  save "$1" "$id"
  echo "$id"
}

log "Subnets"
PUB_A=$(subnet PUBLIC_SUBNET_A "$PUBLIC_SUBNET_A_CIDR" "$AZ_A" public-a)
PUB_B=$(subnet PUBLIC_SUBNET_B "$PUBLIC_SUBNET_B_CIDR" "$AZ_B" public-b)
PRIV_A=$(subnet PRIVATE_SUBNET_A "$PRIVATE_SUBNET_A_CIDR" "$AZ_A" private-a)
PRIV_B=$(subnet PRIVATE_SUBNET_B "$PRIVATE_SUBNET_B_CIDR" "$AZ_B" private-b)
# We attach Elastic IPs explicitly; don't hand out random public IPs (they cost money too).
for s in "$PUB_A" "$PUB_B"; do
  aws ec2 modify-subnet-attribute --region "$AWS_REGION" --subnet-id "$s" --no-map-public-ip-on-launch
done

log "Public route table (0.0.0.0/0 -> Internet Gateway)"
PUBLIC_RT=$(aws ec2 create-route-table --region "$AWS_REGION" --vpc-id "$VPC_ID" \
  --tag-specifications "$(tags route-table public-rt)" --query RouteTable.RouteTableId --output text)
aws ec2 create-route --region "$AWS_REGION" --route-table-id "$PUBLIC_RT" \
  --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW_ID" >/dev/null
for s in "$PUB_A" "$PUB_B"; do
  aws ec2 associate-route-table --region "$AWS_REGION" --route-table-id "$PUBLIC_RT" --subnet-id "$s" >/dev/null
done
save PUBLIC_RT "$PUBLIC_RT"

log "Private route table (default route added after the NAT instance exists)"
PRIVATE_RT=$(aws ec2 create-route-table --region "$AWS_REGION" --vpc-id "$VPC_ID" \
  --tag-specifications "$(tags route-table private-rt)" --query RouteTable.RouteTableId --output text)
for s in "$PRIV_A" "$PRIV_B"; do
  aws ec2 associate-route-table --region "$AWS_REGION" --route-table-id "$PRIVATE_RT" --subnet-id "$s" >/dev/null
done
save PRIVATE_RT "$PRIVATE_RT"

log "Free S3 gateway endpoint (S3 traffic, incl. ECR image layers, bypasses the NAT)"
S3_EP=$(aws ec2 create-vpc-endpoint --region "$AWS_REGION" --vpc-id "$VPC_ID" \
  --service-name "com.amazonaws.$AWS_REGION.s3" --vpc-endpoint-type Gateway \
  --route-table-ids "$PRIVATE_RT" "$PUBLIC_RT" --query VpcEndpoint.VpcEndpointId --output text)
save S3_ENDPOINT "$S3_EP"

log "Done"
aws ec2 describe-subnets --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[].[Tags[?Key==`Name`]|[0].Value,CidrBlock,AvailabilityZone,SubnetId]' --output table
