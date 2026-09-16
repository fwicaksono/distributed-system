#!/usr/bin/env bash
# Executed ON an EC2 host through AWS Systems Manager Run Command (sent by GitHub Actions).
# CI substitutes __ROLE__ and __IMAGE_TAG__ before sending.
#
# Steps: log in to ECR with the instance role -> pull the image -> extract the deployment
# bundle baked into the image -> hand over to that release's deploy.sh.
#
# No SSH, no inbound port, no long-lived AWS keys: GitHub assumes an IAM role via OIDC,
# SSM delivers this script over the agent's outbound connection.
set -euo pipefail

ROLE="__ROLE__"
IMAGE_TAG="__IMAGE_TAG__"
BASE=/opt/taskplatform

# shellcheck disable=SC1091
source "$BASE/shared/node.env"
IMAGE="$ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG"

echo "==> [$ROLE] logging in to $ECR_REGISTRY"
aws ecr get-login-password --region "$AWS_REGION" |
  docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null

echo "==> [$ROLE] pulling $IMAGE"
docker pull --quiet "$IMAGE"

echo "==> [$ROLE] extracting deployment bundle"
release="$BASE/releases/$IMAGE_TAG"
rm -rf "$release.tmp" && mkdir -p "$release.tmp"
cid=$(docker create "$IMAGE")
docker cp "$cid:/opt/bundle/." "$release.tmp/"
docker rm -f "$cid" >/dev/null
chmod +x "$release.tmp"/deploy/scripts/*.sh
rm -rf "$release" && mv "$release.tmp" "$release"

echo "==> [$ROLE] deploying"
exec "$release/deploy/scripts/deploy.sh" "$ROLE" "$IMAGE_TAG"
