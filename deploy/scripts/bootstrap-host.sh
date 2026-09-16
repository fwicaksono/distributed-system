#!/usr/bin/env bash
# One-time preparation of a fresh Ubuntu 24.04 EC2 instance. Idempotent: safe to re-run.
#
#   sudo ./bootstrap-host.sh <role> <node.env path>
#
# Installs Docker Engine + Compose plugin + AWS CLI v2, configures log rotation, swap,
# kernel/network sysctls, ufw, the data volume (data/broker/ops) and systemd units.
set -euo pipefail

ROLE=${1:?usage: bootstrap-host.sh <edge|app|broker|ops|data> <node.env>}
NODE_ENV=${2:?usage: bootstrap-host.sh <role> <node.env>}
BASE=/opt/taskplatform
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

log "base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq ca-certificates curl gnupg jq unzip htop sysstat iotop net-tools \
  dnsutils tcpdump ufw chrony

log "Docker Engine (official apt repository)"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

log "Docker daemon defaults: log rotation + live-restore"
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true,
  "default-address-pools": [{ "base": "172.28.0.0/16", "size": 24 }]
}
EOF
systemctl enable --now docker
systemctl restart docker

log "AWS CLI v2"
if ! command -v aws >/dev/null; then
  arch=$(uname -m)
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${arch}.zip" -o /tmp/awscliv2.zip
  unzip -q -o /tmp/awscliv2.zip -d /tmp && /tmp/aws/install --update
  rm -rf /tmp/aws /tmp/awscliv2.zip
fi

log "swap (1 GiB): cushions short memory spikes on 2 GiB instances"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "kernel tuning"
cat > /etc/sysctl.d/99-taskplatform.conf <<'EOF'
# Prefer RAM over swap; swap only under real pressure.
vm.swappiness = 10
# Larger accept backlog for bursts of new connections (Nginx / uvicorn).
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
# More ephemeral ports for many outbound connections (Nginx -> APIs, APIs -> DB).
net.ipv4.ip_local_port_range = 10240 65535
# Redis recommends this to avoid fork/allocation failures.
vm.overcommit_memory = 1
EOF
sysctl --system >/dev/null

log "directories"
install -d -m 755 "$BASE" "$BASE/releases" "$BASE/bin"
install -d -m 700 "$BASE/shared"
install -m 600 "$NODE_ENV" "$BASE/shared/node.env"
grep -q "^ROLE=$ROLE$" "$BASE/shared/node.env" || { echo "ROLE in node.env must be $ROLE"; exit 1; }

if [[ "$ROLE" =~ ^(data|broker|ops)$ ]]; then
  log "data directory /data"
  # If a second EBS volume is attached (recommended for 'data'), format + mount it once.
  # Nitro instances expose EBS as NVMe: find the unmounted, unpartitioned disk.
  if ! mountpoint -q /data; then
    dev=$(lsblk -dpno NAME,TYPE,MOUNTPOINT | awk '$2=="disk" && $3=="" {print $1}' | while read -r d; do
      [[ -z "$(lsblk -no MOUNTPOINT "$d" | tr -d '[:space:]')" && -z "$(blkid "$d" 2>/dev/null)" ]] && echo "$d"
    done | head -n1 || true)
    if [[ -n "${dev:-}" ]]; then
      echo "formatting $dev as ext4 and mounting at /data"
      mkfs.ext4 -q -L taskdata "$dev"
      install -d /data
      # nofail: the instance still boots if the volume is detached.
      grep -q 'LABEL=taskdata' /etc/fstab || echo 'LABEL=taskdata /data ext4 defaults,nofail,noatime 0 2' >> /etc/fstab
      mount /data
    else
      echo "no extra EBS volume found — using the root volume for /data"
      install -d /data
    fi
  fi
  install -d -o 999 -g 999 /data/rabbitmq          # rabbitmq uid in the alpine image
  install -d -o 70 -g 70 -m 700 /data/postgres     # postgres uid in the alpine image
  install -d -o 65534 -g 65534 /data/prometheus    # nobody
  install -d -o 472 -g 0 /data/grafana             # grafana uid
fi

if [[ "$ROLE" == "edge" ]]; then
  install -d /etc/letsencrypt /var/lib/letsencrypt /var/www/certbot
fi

log "firewall (ufw)"
"$SCRIPT_DIR/../firewall/ufw-rules.sh" "$ROLE" "$BASE/shared/node.env"

log "systemd units"
install -m 644 "$SCRIPT_DIR/../systemd/taskplatform.service" /etc/systemd/system/taskplatform.service
if [[ "$ROLE" == "edge" ]]; then
  install -m 644 "$SCRIPT_DIR/../systemd/certbot-renew.service" /etc/systemd/system/
  install -m 644 "$SCRIPT_DIR/../systemd/certbot-renew.timer" /etc/systemd/system/
  systemctl enable certbot-renew.timer
fi
systemctl daemon-reload
systemctl enable taskplatform.service

log "done — node '$ROLE' is ready for its first deployment"
