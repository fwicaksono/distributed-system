#!/usr/bin/env bash
# Run deploy/scripts/remote-deploy.sh on every instance with tag Role=<role> via SSM Run
# Command, wait for completion, print output, and fail the job if any instance failed.
#
#   .github/scripts/ssm-deploy.sh <role> <image-tag> [target-role]
#     role:         what to deploy (edge|app|broker|ops|data|migrate)
#     target-role:  which instances to run on (default: same as role; migrate -> app)
set -euo pipefail

ROLE=${1:?role}
TAG=${2:?image tag}
TARGET_ROLE=${3:-$ROLE}
: "${AWS_REGION:?}"

script=$(sed -e "s/__ROLE__/$ROLE/" -e "s/__IMAGE_TAG__/$TAG/" deploy/scripts/remote-deploy.sh)
params=$(jq -n --arg s "$script" '{commands: ($s | split("\n")), executionTimeout: ["900"]}')

echo "::group::SSM send-command role=$ROLE targets=tag:Role=$TARGET_ROLE"
command_id=$(aws ssm send-command \
  --region "$AWS_REGION" \
  --document-name AWS-RunShellScript \
  --comment "deploy $ROLE $TAG (${GITHUB_RUN_ID:-local})" \
  --targets "Key=tag:Project,Values=taskplatform" "Key=tag:Role,Values=$TARGET_ROLE" \
  --max-concurrency 1 --max-errors 0 \
  --timeout-seconds 600 \
  --parameters "$params" \
  --query Command.CommandId --output text)
echo "command id: $command_id"
echo "::endgroup::"

# Poll until the command reaches a terminal state on all targets.
for _ in $(seq 1 180); do
  status=$(aws ssm list-commands --region "$AWS_REGION" --command-id "$command_id" \
    --query 'Commands[0].Status' --output text)
  case "$status" in
    Success|Failed|Cancelled|TimedOut) break ;;
  esac
  sleep 5
done

aws ssm list-command-invocations --region "$AWS_REGION" --command-id "$command_id" --details \
  --query 'CommandInvocations[].{instance:InstanceId,status:Status,output:CommandPlugins[0].Output}' \
  --output json | jq -r '.[] | "----- \(.instance): \(.status)\n\(.output)"'

targets=$(aws ssm list-command-invocations --region "$AWS_REGION" --command-id "$command_id" \
  --query 'length(CommandInvocations)' --output text)
if [[ "$targets" == "0" ]]; then
  echo "::error::no instances matched tag Role=$TARGET_ROLE (is the SSM agent online?)"
  exit 1
fi
if [[ "$status" != "Success" ]]; then
  echo "::error::deploy of $ROLE finished with status $status"
  exit 1
fi
