#!/usr/bin/env bash
# Horizontal scaling experiment: same load test against 1, 2 and 3 API servers.
#
#   scripts/scale-experiment.sh [normal|high] [rate]
#
# Results land in loadtest/results/ and a comparison table is printed at the end.
#
# IMPORTANT when interpreting local results: all "servers" share your laptop's CPU cores,
# PostgreSQL included. Adding API containers only helps while the API tier is the
# bottleneck. On EC2 each API node has its own CPU — run the same test there and compare.
set -euo pipefail
cd "$(dirname "$0")/.."

TEST=${1:-normal}
RATE=${2:-150}
C="docker compose"
mkdir -p loadtest/results

for n in 1 2 3; do
  echo -e "\n===================== $n API server(s) ====================="
  cp "nginx/upstreams/local-$n.conf" nginx/upstreams/active.conf
  $C up -d api-1 api-2 api-3
  # Stop the APIs not in this round so they can't receive traffic via stale connections.
  for i in 1 2 3; do (( i > n )) && $C stop "api-$i"; done
  $C exec nginx nginx -t -q && $C exec nginx nginx -s reload
  sleep 5
  $C --profile loadtest run --rm -e API_COUNT="$n" -e RATE="$RATE" -e DURATION=2m k6 run --quiet "/scripts/$TEST.js" \
    | tee "loadtest/results/$TEST-${n}api.txt" | grep -E '^scenario=' || true
done

echo -e "\n===================== summary ====================="
grep -h '^scenario=' loadtest/results/"$TEST"-*api.txt | column -t

# Restore the default 3-server configuration.
cp nginx/upstreams/local-3.conf nginx/upstreams/active.conf
$C up -d api-1 api-2 api-3 && $C exec nginx nginx -s reload
