#!/usr/bin/env bash
# Key pair, NAT instance, the five application instances, data EBS volume, Elastic IP.
source "$(dirname "$0")/00-config.sh"
HERE="$(cd "$(dirname "$0")" && pwd)"

ami() { aws ssm get-parameter --region "$AWS_REGION" --name "$1" --query Parameter.Value --output text; }
UBUNTU_AMI=$(ami "$UBUNTU_AMI_PARAM")
AL2023_ARM_AMI=$(ami "$AL2023_ARM_AMI_PARAM")

log "SSH key pair ($KEY_NAME) — private key saved to ~/.ssh/$KEY_NAME.pem (0400)"
if ! aws ec2 describe-key-pairs --region "$AWS_REGION" --key-names "$KEY_NAME" >/dev/null 2>&1; then
  aws ec2 create-key-pair --region "$AWS_REGION" --key-name "$KEY_NAME" --key-type ed25519 \
    --query KeyMaterial --output text > ~/.ssh/"$KEY_NAME".pem
  chmod 400 ~/.ssh/"$KEY_NAME".pem
fi

launch() {  # launch <key> <name> <role> <type> <ami> <subnet> <sg> <private-ip> [extra args...]
  local key=$1 name=$2 role=$3 type=$4 image=$5 subnet=$6 sg=$7 ip=$8; shift 8
  local id
  id=$(aws ec2 run-instances --region "$AWS_REGION" \
    --image-id "$image" --instance-type "$type" --key-name "$KEY_NAME" \
    --subnet-id "$subnet" --security-group-ids "$sg" --private-ip-address "$ip" \
    --iam-instance-profile Name="$(load INSTANCE_PROFILE)" \
    `# IMDSv2 only; hop limit 1 = containers on Docker bridge networks cannot reach the` \
    `# metadata service, so a compromised container cannot steal the instance role.` \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=1" \
    --credit-specification CpuCredits=standard \
    --tag-specifications \
      "ResourceType=instance,Tags=[{Key=Name,Value=$PROJECT-$name},{Key=Project,Value=$PROJECT},{Key=Role,Value=$role}]" \
      "ResourceType=volume,Tags=[{Key=Name,Value=$PROJECT-$name-root},{Key=Project,Value=$PROJECT}]" \
    "$@" --query 'Instances[0].InstanceId' --output text)
  save "$key" "$id"
  echo "  $name ($type, $ip): $id"
}
ROOT_20G='[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}}]'

log "NAT instance (Amazon Linux 2023, ARM) in the public subnet"
launch NAT_INSTANCE nat nat "$NAT_TYPE" "$AL2023_ARM_AMI" "$(load PUBLIC_SUBNET_A)" "$(load SG_NAT)" "$NAT_IP" \
  --user-data "file://$HERE/nat-instance-user-data.sh"
NAT_ID=$(load NAT_INSTANCE)
aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$NAT_ID"
# A NAT forwards packets whose source/destination is NOT itself — EC2 drops those by default.
aws ec2 modify-instance-attribute --region "$AWS_REGION" --instance-id "$NAT_ID" --no-source-dest-check
NAT_EIP=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc \
  --tag-specifications "$(tags elastic-ip nat-eip)" --query AllocationId --output text)
aws ec2 associate-address --region "$AWS_REGION" --instance-id "$NAT_ID" --allocation-id "$NAT_EIP" >/dev/null
save NAT_EIP_ALLOC "$NAT_EIP"
aws ec2 create-route --region "$AWS_REGION" --route-table-id "$(load PRIVATE_RT)" \
  --destination-cidr-block 0.0.0.0/0 --instance-id "$NAT_ID" >/dev/null
echo "  private route table: 0.0.0.0/0 -> $NAT_ID"

log "Application instances (Ubuntu 24.04, encrypted gp3 root volumes, IMDSv2 required)"
launch EDGE_INSTANCE   edge   edge   "$EDGE_TYPE"   "$UBUNTU_AMI" "$(load PUBLIC_SUBNET_A)"  "$(load SG_EDGE)"   "$EDGE_IP"   --block-device-mappings "$ROOT_20G"
launch APP_INSTANCE    app    app    "$APP_TYPE"    "$UBUNTU_AMI" "$(load PRIVATE_SUBNET_A)" "$(load SG_APP)"    "$APP_IP"    --block-device-mappings "$ROOT_20G"
launch BROKER_INSTANCE broker broker "$BROKER_TYPE" "$UBUNTU_AMI" "$(load PRIVATE_SUBNET_A)" "$(load SG_BROKER)" "$BROKER_IP" --block-device-mappings "$ROOT_20G"
launch OPS_INSTANCE    ops    ops    "$OPS_TYPE"    "$UBUNTU_AMI" "$(load PRIVATE_SUBNET_A)" "$(load SG_OPS)"    "$OPS_IP"    --block-device-mappings "$ROOT_20G"
# Data node: root volume + a SEPARATE 20 GiB data volume that survives instance termination.
DATA_DISKS='[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}},{"DeviceName":"/dev/sdf","Ebs":{"VolumeSize":20,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":false}}]'
launch DATA_INSTANCE   data   data   "$DATA_TYPE"   "$UBUNTU_AMI" "$(load PRIVATE_SUBNET_A)" "$(load SG_DATA)"   "$DATA_IP"   --block-device-mappings "$DATA_DISKS"

aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids \
  "$(load EDGE_INSTANCE)" "$(load APP_INSTANCE)" "$(load BROKER_INSTANCE)" "$(load OPS_INSTANCE)" "$(load DATA_INSTANCE)"

log "Elastic IP for the edge node (stable public address for DNS)"
EDGE_EIP=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc \
  --tag-specifications "$(tags elastic-ip edge-eip)" --query AllocationId --output text)
aws ec2 associate-address --region "$AWS_REGION" --instance-id "$(load EDGE_INSTANCE)" --allocation-id "$EDGE_EIP" >/dev/null
save EDGE_EIP_ALLOC "$EDGE_EIP"
EDGE_PUBLIC_IP=$(aws ec2 describe-addresses --region "$AWS_REGION" --allocation-ids "$EDGE_EIP" \
  --query 'Addresses[0].PublicIp' --output text)
save EDGE_PUBLIC_IP "$EDGE_PUBLIC_IP"

log "Done"
echo "Edge public IP: $EDGE_PUBLIC_IP  ->  create DNS record:  $DOMAIN  A  $EDGE_PUBLIC_IP"
cat <<EOF

~/.ssh/config snippet (edge as bastion / jump host):

Host tp-edge
  HostName $EDGE_PUBLIC_IP
  User ubuntu
  IdentityFile ~/.ssh/$KEY_NAME.pem
Host tp-app tp-broker tp-ops tp-data
  User ubuntu
  IdentityFile ~/.ssh/$KEY_NAME.pem
  ProxyJump tp-edge
Host tp-app
  HostName $APP_IP
Host tp-broker
  HostName $BROKER_IP
Host tp-ops
  HostName $OPS_IP
Host tp-data
  HostName $DATA_IP
EOF
