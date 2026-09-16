#!/usr/bin/env bash
# "Breaking the System" — failure injection for the LOCAL Docker Compose stack.
#
#   scripts/chaos.sh <scenario>        scripts/chaos.sh list
#
# Run a load test in another terminal first (normal.js or task-processing.js) and keep
# Grafana (http://localhost:3000) open: every scenario is only meaningful while traffic flows.
set -euo pipefail
cd "$(dirname "$0")/.."

C="docker compose"
say() { printf '\n\033[1;33m[chaos] %s\033[0m\n' "$*"; }
wait_key() { read -r -p "Press ENTER to recover..." _; }
probe() {  # probe <n>: send n requests through Nginx and summarize status codes/instances
  for _ in $(seq "${1:-20}"); do
    curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 http://localhost:8080/health/live || echo "000"
  done | sort | uniq -c
}

# Adds latency to all traffic leaving a container, using a throwaway sidecar that joins
# the container's network namespace (no NET_ADMIN needed in the app image).
netem() {  # netem <service> <args...>
  local cid; cid=$($C ps -q "$1")
  docker run --rm --net "container:$cid" --cap-add NET_ADMIN nicolaka/netshoot:v0.13 \
    tc qdisc "${@:2}"
}

scenario=${1:-list}
case "$scenario" in
  list)
    cat <<'EOF'
Scenarios:
  kill-api          docker kill api-1 (SIGKILL, no graceful shutdown)
  kill-two-apis     kill api-1 and api-2 — one API left carries all traffic
  kill-all-apis     full API outage — Nginx returns 502
  kill-worker       kill worker-1 while tasks are in flight — redelivery + lease
  kill-all-workers  no consumers — queue grows, API still answers 202
  stop-redis        cache + rate limiter disappear — fail-open behaviour
  stop-rabbitmq     broker down — POST /process returns 503, reads keep working
  stop-postgres     source of truth down — readiness fails, most endpoints 5xx
  restart-postgres  short DB restart — pool pre-ping recovers without API restarts
  latency-db        +300 ms latency on PostgreSQL — timeouts & pool exhaustion
  latency-api       +200 ms latency on api-1 only — one slow node in the pool
  cpu-api           burn CPU inside api-1 (container limited to 1 CPU)
  memory-worker     allocate memory in worker-1 until the 512M limit -> OOM kill
  restart-all       restart every container — dependency ordering & recovery
EOF
    ;;

  kill-api)
    say "Killing api-1 with SIGKILL"; $C kill api-1
    say "Nginx: first requests to api-1 fail -> proxy_next_upstream retries on api-2/api-3"
    probe 30
    say "Docker treats 'kill' as a MANUAL stop: the restart policy does NOT bring api-1 back."
    say "(A real crash or an OOM kill DOES trigger 'restart: unless-stopped' — see memory-worker.)"
    $C ps -a api-1
    wait_key; $C up -d api-1
    ;;

  kill-two-apis)
    say "Killing api-1 and api-2"; $C kill api-1 api-2
    probe 30
    say "Watch Grafana 'Requests / s per API instance': api-3 now takes 100%"
    wait_key; $C up -d api-1 api-2
    ;;

  kill-all-apis)
    say "Stopping all APIs"; $C stop api-1 api-2 api-3
    probe 10
    say "Expect 502 Bad Gateway: Nginx is alive, nothing upstream is. Queue + workers keep draining."
    wait_key; $C start api-1 api-2 api-3
    ;;

  kill-worker)
    say "Killing worker-1 mid-processing (SIGKILL: no ack is ever sent)"
    $C kill worker-1
    say "RabbitMQ requeues worker-1's unacked messages -> worker-2 receives them (redelivered=true)."
    say "Tasks worker-1 had CLAIMED stay 'processing' until the lease expires (60s), then are re-claimed."
    docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready messages_unacknowledged consumers
    wait_key; $C up -d worker-1
    ;;

  kill-all-workers)
    say "Stopping all workers"; $C stop worker-1 worker-2
    say "POST /tasks/{id}/process still returns 202 — messages are durable in RabbitMQ."
    for _ in 1 2 3 4 5 6; do
      docker compose exec -T rabbitmq rabbitmqctl -q list_queues name messages_ready consumers | grep tasks.process
      sleep 5
    done
    wait_key; $C start worker-1 worker-2
    say "Workers drain the backlog at (2 workers x prefetch 4) concurrent tasks."
    ;;

  stop-redis)
    say "Stopping Redis"; $C stop redis
    say "Expect: requests still succeed (cache miss -> PostgreSQL), latency slightly up,"
    say "cache_operations_total{result=\"error\"} rising, rate limiting disabled (fail-open)."
    probe 10
    wait_key; $C start redis
    ;;

  stop-rabbitmq)
    say "Stopping RabbitMQ"; $C stop rabbitmq
    say "Expect: GET endpoints OK; POST /process -> 503 after the publish timeout; task marked failed."
    say "Workers log reconnect attempts (connect_robust). /health/ready reports 'degraded'."
    curl -s http://localhost:8080/health/ready | python3 -m json.tool || true
    wait_key; $C start rabbitmq
    ;;

  stop-postgres)
    say "Stopping PostgreSQL"; $C stop postgres
    say "Expect: /health/ready -> 503 on every API; authenticated endpoints -> 500/503;"
    say "workers nack+requeue messages; nothing is lost, nothing progresses."
    curl -s -w '\nHTTP %{http_code}\n' http://localhost:8080/health/ready || true
    wait_key; $C start postgres
    ;;

  restart-postgres)
    say "Restarting PostgreSQL (~3s of downtime)"; $C restart postgres
    say "In-flight requests fail; pool_pre_ping discards dead connections; no API restart needed."
    probe 10
    ;;

  latency-db)
    say "Adding 300ms latency to PostgreSQL egress"
    netem postgres add dev eth0 root netem delay 300ms 50ms
    say "Each query now costs >= 300ms. Pool connections are held longer -> pool exhaustion ->"
    say "requests wait DB_POOL_TIMEOUT_SECONDS (5s) then fail. Watch db_pool_connections_in_use."
    wait_key; netem postgres del dev eth0 root
    ;;

  latency-api)
    say "Adding 200ms latency to api-1 only"
    netem api-1 add dev eth0 root netem delay 200ms
    say "Round robin still sends 1/3 of traffic to the slow node: p95/p99 rise for everyone."
    say "least_conn would shift load away from it; outlier detection is a service-mesh feature."
    wait_key; netem api-1 del dev eth0 root
    ;;

  cpu-api)
    say "Burning CPU inside api-1 for 60s (container limit: 1 CPU)"
    $C exec -d api-1 python -c "import time; e=time.time()+60
while time.time()<e: pass"
    say "The event loop competes for the 1-CPU quota -> api-1 latency rises (CFS throttling)."
    say "Health checks may time out -> Docker marks it unhealthy (but does not restart it)."
    ;;

  memory-worker)
    say "Allocating memory inside worker-1 beyond its 512M limit"
    $C exec -d worker-1 python -c "b=[]
import time
while True: b.append(bytearray(50*1024*1024)); time.sleep(0.2)"
    sleep 8
    docker inspect "$($C ps -aq worker-1)" --format 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} Restarts={{.RestartCount}}'
    say "The cgroup hits memory.max -> the kernel OOM killer kills the LARGEST process in it:"
    say "here the allocating python process. Had it been the worker (PID 1), the container would"
    say "exit with 137 (128+SIGKILL), OOMKilled=true, and 'restart: unless-stopped' would restart it."
    $C logs --tail=5 worker-1
    ;;

  restart-all)
    say "Restarting the whole stack"; $C restart
    say "Services with depends_on + healthchecks start in order; the rest retry with backoff."
    sleep 20; $C ps
    ;;

  *) echo "unknown scenario '$scenario' (try: scripts/chaos.sh list)"; exit 1 ;;
esac
