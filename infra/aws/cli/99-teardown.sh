#!/usr/bin/env bash
# Delete EVERYTHING created by 01-04, in dependency order. Stops all charges for this project.
#
#   ./99-teardown.sh            (asks for confirmation)
#
# Order matters: instances -> Elastic IPs -> detached volumes -> NAT route -> SGs ->
# endpoints -> subnets/route tables -> IGW -> VPC -> ECR -> SSM params -> IAM.
source "$(dirname "$0")/00-config.sh"

read -r -p "Type 'destroy $PROJECT' to delete all $PROJECT resources in $AWS_REGION: " answer
[[ "$answer" == "destroy $PROJECT" ]] || { echo "aborted"; exit 1; }

try() { "$@" 2>/dev/null || true; }
VPC_ID=$(load VPC_ID)

log "Terminating instances"
ids=$(aws ec2 describe-instances --region "$AWS_REGION" \
  --filters "Name=tag:Project,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
if [[ -n "$ids" ]]; then
  # shellcheck disable=SC2086
  aws ec2 terminate-instances --region "$AWS_REGION" --instance-ids $ids >/dev/null
  # shellcheck disable=SC2086
  aws ec2 wait instance-terminated --region "$AWS_REGION" --instance-ids $ids
fi

log "Releasing Elastic IPs (an unattached EIP is billed hourly)"
for alloc in $(aws ec2 describe-addresses --region "$AWS_REGION" \
    --filters "Name=tag:Project,Values=$PROJECT" --query 'Addresses[].AllocationId' --output text); do
  try aws ec2 release-address --region "$AWS_REGION" --allocation-id "$alloc"
done

log "Deleting leftover EBS volumes (the data volume has DeleteOnTermination=false)"
# Only volumes tagged Project=taskplatform — never touch other volumes in the account.
for vol in $(aws ec2 describe-volumes --region "$AWS_REGION" \
    --filters "Name=status,Values=available" "Name=tag:Project,Values=$PROJECT" \
    --query 'Volumes[].VolumeId' --output text); do
  echo "  deleting $vol"
  try aws ec2 delete-volume --region "$AWS_REGION" --volume-id "$vol"
done

if [[ -n "$VPC_ID" ]]; then
  log "Security groups"
  for sg in $(aws ec2 describe-security-groups --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text); do
    # Rules referencing other SGs must be revoked before those SGs can be deleted.
    perms=$(aws ec2 describe-security-groups --region "$AWS_REGION" --group-ids "$sg" --query 'SecurityGroups[0].IpPermissions')
    [[ "$perms" != "[]" ]] && try aws ec2 revoke-security-group-ingress --region "$AWS_REGION" --group-id "$sg" --ip-permissions "$perms"
  done
  for sg in $(aws ec2 describe-security-groups --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text); do
    try aws ec2 delete-security-group --region "$AWS_REGION" --group-id "$sg"
  done

  log "VPC endpoints, subnets, route tables, internet gateway, VPC"
  for ep in $(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'VpcEndpoints[].VpcEndpointId' --output text); do
    try aws ec2 delete-vpc-endpoints --region "$AWS_REGION" --vpc-endpoint-ids "$ep"
  done
  for subnet in $(aws ec2 describe-subnets --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'Subnets[].SubnetId' --output text); do
    try aws ec2 delete-subnet --region "$AWS_REGION" --subnet-id "$subnet"
  done
  for rt in $(aws ec2 describe-route-tables --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'RouteTables[?Associations[0].Main!=`true`].RouteTableId' --output text); do
    try aws ec2 delete-route-table --region "$AWS_REGION" --route-table-id "$rt"
  done
  IGW_ID=$(load IGW_ID)
  if [[ -n "$IGW_ID" ]]; then
    try aws ec2 detach-internet-gateway --region "$AWS_REGION" --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
    try aws ec2 delete-internet-gateway --region "$AWS_REGION" --internet-gateway-id "$IGW_ID"
  fi
  try aws ec2 delete-vpc --region "$AWS_REGION" --vpc-id "$VPC_ID"
fi

log "ECR repository (and all images)"
try aws ecr delete-repository --region "$AWS_REGION" --repository-name "$PROJECT" --force

log "SSM parameters"
names=$(aws ssm get-parameters-by-path --region "$AWS_REGION" --path "/$PROJECT/$ENVIRONMENT" \
  --recursive --query 'Parameters[].Name' --output text)
# shellcheck disable=SC2086
[[ -n "$names" ]] && try aws ssm delete-parameters --region "$AWS_REGION" --names $names

log "IAM roles and instance profile"
try aws iam remove-role-from-instance-profile --instance-profile-name "$PROJECT-ec2-profile" --role-name "$PROJECT-ec2-role"
try aws iam delete-instance-profile --instance-profile-name "$PROJECT-ec2-profile"
try aws iam detach-role-policy --role-name "$PROJECT-ec2-role" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
try aws iam delete-role-policy --role-name "$PROJECT-ec2-role" --policy-name "$PROJECT-ec2-inline"
try aws iam delete-role --role-name "$PROJECT-ec2-role"
try aws iam delete-role-policy --role-name "$PROJECT-github-deploy" --policy-name "$PROJECT-deploy-inline"
try aws iam delete-role --role-name "$PROJECT-github-deploy"
echo "  (the GitHub OIDC provider is account-wide and free; delete it manually if unused)"

try aws ec2 delete-key-pair --region "$AWS_REGION" --key-name "$KEY_NAME"
mv "$STATE_FILE" "$STATE_FILE.destroyed.$(date +%s)"

log "Teardown complete. Verify in the console: EC2 (instances, volumes, EIPs), VPC, ECR, Billing."
