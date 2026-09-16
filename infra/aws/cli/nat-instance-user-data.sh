#!/bin/bash
# cloud-init user data for the NAT instance (Amazon Linux 2023). Runs once at first boot.
#
# What a NAT does: private instances send packets to 0.0.0.0/0 -> this instance (route
# table). It rewrites the SOURCE address to its own (MASQUERADE) and forwards to the
# internet; replies come back to it and are translated back. Inbound connections that
# were not initiated from inside are impossible — that is what keeps private subnets private.
set -euxo pipefail

dnf install -y iptables-services

# 1. Let the kernel forward packets between interfaces.
cat > /etc/sysctl.d/90-nat.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.send_redirects = 0
EOF
sysctl --system

# 2. Translate source addresses of forwarded traffic leaving the primary interface.
IFACE=$(ip route show default | awk '{print $5; exit}')
iptables -t nat -A POSTROUTING -o "$IFACE" -s 10.20.0.0/16 -j MASQUERADE
iptables -A FORWARD -i "$IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -A FORWARD -s 10.20.0.0/16 -o "$IFACE" -j ACCEPT
iptables-save > /etc/sysconfig/iptables
systemctl enable --now iptables
