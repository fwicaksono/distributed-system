#!/usr/bin/env bash
# Host firewall (ufw) per node role — defense in depth BEHIND the AWS Security Groups.
#
#   sudo ./ufw-rules.sh <role> /opt/taskplatform/shared/node.env
#
# Security Group  = stateful firewall at the AWS hypervisor, per network interface.
#                   Traffic it drops never reaches the instance.
# ufw (iptables)  = firewall inside the Linux kernel of the instance. Protects even if a
#                   Security Group is misconfigured (e.g. someone adds 0.0.0.0/0 on 5432).
#
# !! Docker caveat: ports PUBLISHED by Docker (`ports:` in compose) are handled by Docker's
# own iptables chains (DOCKER / DOCKER-USER), which are evaluated BEFORE ufw's rules.
# ufw does NOT protect published container ports. That is why every internal service is
# published on the PRIVATE IP only (never 0.0.0.0) and why Security Groups remain the
# primary control. Host-network services (node-exporter) ARE filtered by ufw.
set -euo pipefail

ROLE=${1:?usage: ufw-rules.sh <role> <node.env>}
# shellcheck disable=SC1090
source "${2:?usage: ufw-rules.sh <role> <node.env>}"
VPC_CIDR=${VPC_CIDR:-10.20.0.0/16}

ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing          # updates, ECR, SSM, NTP (SGs restrict egress if needed)
ufw logging low

allow_from() {  # allow_from <source> <port> <comment>
  ufw allow proto tcp from "$1" to any port "$2" comment "$3"
}

case "$ROLE" in
  edge)
    ufw allow 80/tcp  comment 'HTTP (ACME + redirect)'
    ufw allow 443/tcp comment 'HTTPS'
    # SSH: in AWS the SG restricts 22 to YOUR IP; here we allow it and rely on the SG,
    # because your home IP changes. Prefer SSM Session Manager and remove this rule.
    ufw limit 22/tcp  comment 'SSH (rate limited: 6 conn / 30 s per IP)'
    allow_from "$OPS_IP" 9100 'node-exporter from ops'
    ;;
  app|broker|ops|data)
    allow_from "$EDGE_IP" 22 'SSH via bastion (edge) only'
    allow_from "$OPS_IP" 9100 'node-exporter from ops'
    ;;
  *) echo "unknown role $ROLE"; exit 1 ;;
esac

# Documented for clarity; enforced by SG + private-IP binding (see Docker caveat above).
case "$ROLE" in
  app)    echo "# container ports 8000-8001 <- edge ($EDGE_IP), 8000 <- ops" ;;
  broker) echo "# container ports 5672 <- edge/app/ops, 15692,9101 <- ops" ;;
  data)   echo "# container ports 5432,6379 <- edge/app/broker/ops, 9187,9121 <- ops" ;;
esac

ufw --force enable
ufw status verbose
