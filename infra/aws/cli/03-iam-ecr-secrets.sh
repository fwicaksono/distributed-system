#!/usr/bin/env bash
# IAM roles (no long-lived access keys anywhere), ECR repository, SSM Parameter Store secrets.
#
#   EC2 instances  --(instance profile)-->  taskplatform-ec2-role  : pull images, read secrets, SSM agent
#   GitHub Actions --(OIDC federation)--->  taskplatform-github-deploy : push images, send SSM commands
source "$(dirname "$0")/00-config.sh"
IAM_DIR="$(cd "$(dirname "$0")/../iam" && pwd)"

render() {  # substitute placeholders in a policy template
  sed -e "s#__AWS_REGION__#$AWS_REGION#g" -e "s#__AWS_ACCOUNT_ID__#$AWS_ACCOUNT_ID#g" \
      -e "s#__GITHUB_REPO__#$GITHUB_REPO#g" "$IAM_DIR/$1"
}

log "EC2 instance role"
aws iam create-role --role-name "$PROJECT-ec2-role" \
  --assume-role-policy-document "$(render ec2-trust-policy.json)" \
  --description "Task platform EC2 hosts" --tags Key=Project,Value="$PROJECT" >/dev/null
aws iam put-role-policy --role-name "$PROJECT-ec2-role" --policy-name "$PROJECT-ec2-inline" \
  --policy-document "$(render ec2-instance-policy.json)"
# AWS-managed policy for the SSM agent: Session Manager (shell without SSH) + Run Command.
aws iam attach-role-policy --role-name "$PROJECT-ec2-role" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name "$PROJECT-ec2-profile" >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name "$PROJECT-ec2-profile" \
  --role-name "$PROJECT-ec2-role"
save INSTANCE_PROFILE "$PROJECT-ec2-profile"

log "GitHub OIDC identity provider (once per AWS account)"
if ! aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output text \
     | grep -q token.actions.githubusercontent.com; then
  # AWS validates GitHub's certificate via its trusted CA store; the thumbprint is a legacy
  # required field and is not used for GitHub.
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 >/dev/null
fi

log "GitHub deploy role (assumable only by $GITHUB_REPO main branch / production environment)"
aws iam create-role --role-name "$PROJECT-github-deploy" \
  --assume-role-policy-document "$(render github-oidc-trust-policy.json)" \
  --max-session-duration 3600 --tags Key=Project,Value="$PROJECT" >/dev/null
aws iam put-role-policy --role-name "$PROJECT-github-deploy" --policy-name "$PROJECT-deploy-inline" \
  --policy-document "$(render github-deploy-policy.json)"
save GITHUB_ROLE_ARN "arn:aws:iam::$AWS_ACCOUNT_ID:role/$PROJECT-github-deploy"

log "ECR repository (immutable tags, scan on push, keep last 20 images)"
aws ecr create-repository --region "$AWS_REGION" --repository-name "$PROJECT" \
  --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true \
  --encryption-configuration encryptionType=AES256 --tags Key=Project,Value="$PROJECT" >/dev/null
aws ecr put-lifecycle-policy --region "$AWS_REGION" --repository-name "$PROJECT" \
  --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"keep last 20 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":20},"action":{"type":"expire"}}]}' >/dev/null
save ECR_REGISTRY "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

log "Secrets in SSM Parameter Store (SecureString, KMS-encrypted with the aws/ssm key)"
rand() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-"$1"; }
put() {  # put <name> <value> <type>
  aws ssm put-parameter --region "$AWS_REGION" --name "/$PROJECT/$ENVIRONMENT/$1" \
    --value "$2" --type "$3" --overwrite >/dev/null
  echo "  /$PROJECT/$ENVIRONMENT/$1 ($3)"
}
put JWT_SECRET_KEY          "$(rand 64)" SecureString
put POSTGRES_PASSWORD       "$(rand 32)" SecureString
put REDIS_PASSWORD          "$(rand 32)" SecureString
put RABBITMQ_PASSWORD       "$(rand 32)" SecureString
put GRAFANA_ADMIN_PASSWORD  "$(rand 24)" SecureString
put POSTGRES_DB             taskplatform String
put POSTGRES_USER           taskplatform String
put RABBITMQ_USER           taskplatform String
put CORS_ALLOWED_ORIGINS    "https://$DOMAIN" String

log "Done. Add these GitHub repository variables (Settings -> Secrets and variables -> Actions -> Variables):"
echo "  AWS_REGION=$AWS_REGION"
echo "  AWS_DEPLOY_ROLE_ARN=$(load GITHUB_ROLE_ARN)"
echo "  ECR_REPOSITORY=$PROJECT"
echo "  PUBLIC_BASE_URL=https://$DOMAIN"
