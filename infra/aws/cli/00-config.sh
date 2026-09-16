#!/usr/bin/env bash
# Shared configuration for all AWS CLI scripts. `source` it; don't execute it.
#
# CHANGE every value marked CHANGE before running anything.
# Resource IDs created by the scripts are appended to infra/aws/.state (git-ignored),
# so later scripts and the teardown script can find them.

set -euo pipefail

export AWS_REGION="${AWS_REGION:-ap-southeast-1}"   # CHANGE: region closest to you
export AWS_PAGER=""                                  # never open `less` in scripts
export PROJECT="taskplatform"
export ENVIRONMENT="prod"

export GITHUB_REPO="${GITHUB_REPO:-your-github-user/distributed-task-platform}"   # CHANGE
export DOMAIN="${DOMAIN:-api.example.com}"                                        # CHANGE
export KEY_NAME="${KEY_NAME:-taskplatform-admin}"      # EC2 key pair name (created by 04)

# Your public IP, allowed to SSH to the edge node only. Re-run 02 when it changes.
MY_IP="${MY_IP:-$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')}"
export MY_IP

# --- Network plan -------------------------------------------------------------------------
# 10.20.0.0/16 = 65 536 addresses. Chosen instead of 10.0.0.0/16 to avoid colliding with
# home/office/VPN networks if you ever connect them (VPN or peering needs non-overlapping CIDRs).
export VPC_CIDR="10.20.0.0/16"
export AZ_A="${AWS_REGION}a"
export AZ_B="${AWS_REGION}b"
export PUBLIC_SUBNET_A_CIDR="10.20.1.0/24"    # edge (Nginx), NAT instance
export PUBLIC_SUBNET_B_CIDR="10.20.2.0/24"    # reserved: an ALB requires 2 AZs
export PRIVATE_SUBNET_A_CIDR="10.20.11.0/24"  # app, broker, ops, data
export PRIVATE_SUBNET_B_CIDR="10.20.12.0/24"  # reserved: RDS Multi-AZ / 2nd AZ app nodes

# Fixed private IPs = simple service discovery. (AWS reserves .0-.3 and .255 in each subnet.)
export EDGE_IP="10.20.1.10"
export NAT_IP="10.20.1.20"
export APP_IP="10.20.11.20"
export BROKER_IP="10.20.11.30"
export OPS_IP="10.20.11.40"
export DATA_IP="10.20.11.50"

# --- Instance sizing (see tutorial: Cost) --------------------------------------------------
export EDGE_TYPE="t3.small"      # Nginx + 1 API
export APP_TYPE="t3.small"       # 2 APIs
export BROKER_TYPE="t3.small"    # RabbitMQ + 1 worker
export OPS_TYPE="t3.small"       # 1 worker + Prometheus + Grafana
export DATA_TYPE="t3.medium"     # PostgreSQL + Redis (4 GiB RAM)
export NAT_TYPE="t4g.nano"       # NAT instance (ARM, ~$3/month) instead of a ~$33/month NAT Gateway

# Ubuntu 24.04 LTS AMIs, resolved from Canonical's public SSM parameters (always current).
export UBUNTU_AMI_PARAM="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
export AL2023_ARM_AMI_PARAM="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"

STATE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.state"
export STATE_FILE
touch "$STATE_FILE"

# save KEY VALUE   -> persists an ID;   load KEY -> reads it back
save() { sed -i.bak "/^$1=/d" "$STATE_FILE" && rm -f "$STATE_FILE.bak" && echo "$1=$2" >> "$STATE_FILE"; }
load() { grep -E "^$1=" "$STATE_FILE" | tail -n1 | cut -d= -f2-; }
tags() { echo "ResourceType=$1,Tags=[{Key=Name,Value=$PROJECT-$2},{Key=Project,Value=$PROJECT},{Key=Environment,Value=$ENVIRONMENT}]"; }
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export AWS_ACCOUNT_ID
