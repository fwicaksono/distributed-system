# Distributed Task Platform

A deliberately simple task API, built on deliberately real infrastructure.

The business logic is trivial — users create tasks, submit them for background processing, and poll their status. The architecture is the point: a FastAPI service that grows from one process on a laptop into a load-balanced, queue-backed, observable, CI/CD-deployed system spread across five EC2 instances in a private AWS network.

> **Learning material (Bahasa Indonesia, English technical terms):** [`docs/tutorial.html`](docs/tutorial.html) walks from zero to a deployed system in 43 steps (offline, single file, built from `docs/tutorial/src` with `make docs`). [`docs/architecture.html`](docs/architecture.html) is an animated, interactive map of how the pieces work together.

---

## Contents

- [Learning objectives](#learning-objectives)
- [Architecture](#architecture)
- [Technology stack and why each piece exists](#technology-stack)
- [AWS infrastructure and networking](#aws-infrastructure)
- [Request flows](#request-flows)
- [CI/CD](#cicd)
- [Observability](#observability)
- [Failure handling](#failure-handling)
- [Security](#security)
- [Local development](#local-development)
- [AWS deployment overview](#aws-deployment-overview)
- [Configuration](#environment-variables)
- [API](#api-endpoints)
- [Load testing](#load-testing)
- [Repository structure](#repository-structure)
- [Trade-offs, limitations, lessons learned](#trade-offs)
- [Cost](#cost-considerations)

---

## Learning objectives

By building and breaking this system you should be able to explain, with evidence from your own experiments:

| Area | What you will be able to reason about |
|---|---|
| Backend | Layered FastAPI design, dependency injection, validation, error contracts, connection pooling |
| Data | When PostgreSQL vs Redis, transactions and row locks, indexes, keyset pagination, `EXPLAIN ANALYZE` |
| Async processing | Why a queue, at-least-once delivery, acks/nacks, prefetch, retries with backoff, dead-letter queues, idempotency |
| Concurrency | Event loop vs threads vs processes; concurrency vs parallelism; where each is used here |
| Distributed systems | Stateless vs stateful, partial failure, timeouts, retries, consistency trade-offs, single points of failure |
| Networking | VPC, CIDR, public vs private subnets, route tables, IGW, NAT, Security Groups vs host firewalls, DNS, TLS |
| Operations | Linux diagnostics, Docker, systemd, structured logs, metrics, dashboards, alerts |
| Delivery | Immutable images, OIDC to AWS without stored keys, rolling deploys, health checks, rollback |

---

## Architecture

```
                               Internet
                                   │  DNS: api.example.com → Elastic IP
                                   ▼
┌─────────────────────────── VPC 10.20.0.0/16 ─────────────────────────────────┐
│  Public subnet 10.20.1.0/24                                                   │
│  ┌───────────────────────────────────────┐   ┌──────────────────────────┐    │
│  │ edge  10.20.1.10  (sg-edge, EIP)      │   │ nat  10.20.1.20 (sg-nat) │    │
│  │  Nginx :80/:443  TLS + load balancer  │   │  outbound internet for   │    │
│  │  api-1 :8000                          │   │  private subnet          │    │
│  └──────┬───────────────┬────────────────┘   └────────────▲─────────────┘    │
│         │ round robin   │                                 │ 0.0.0.0/0        │
│  ───────┼───────────────┼─────────────────────────────────┼──────────────    │
│  Private subnet 10.20.11.0/24                             │                  │
│         ▼               ▼                                                    │
│  ┌──────────────────────────────┐    ┌──────────────────────────────────┐    │
│  │ app  10.20.11.20  (sg-app)   │    │ broker  10.20.11.30  (sg-broker) │    │
│  │  api-2 :8000   api-3 :8001   │───▶│  RabbitMQ :5672                  │    │
│  └──────────────┬───────────────┘    │  worker-1                        │    │
│                 │                    └──────────────┬───────────────────┘    │
│                 │                                   │ competing consumers    │
│                 ▼                                   ▼                        │
│  ┌──────────────────────────────┐    ┌──────────────────────────────────┐    │
│  │ data  10.20.11.50  (sg-data) │◀───│ ops  10.20.11.40  (sg-ops)       │    │
│  │  PostgreSQL :5432            │    │  worker-2                        │    │
│  │  Redis      :6379            │    │  Prometheus :9090 → Grafana :3000│    │
│  │  /data on separate EBS       │    │  scrapes every node (private IPs)│    │
│  └──────────────────────────────┘    └──────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────────────┘

GitHub ─▶ Actions: lint → test → build → push to ECR ─▶ SSM Run Command ─▶ each node pulls & rolls
```

### Service responsibilities

| Component | Responsibility | State | Scales by |
|---|---|---|---|
| **Nginx** | TLS termination, reverse proxy, round-robin load balancing, passive health checks, edge rate limiting | Stateless (certs on disk) | Bigger instance, or replace with an ALB |
| **FastAPI (api-1..3)** | HTTP contract, auth, validation, orchestration of DB/cache/queue | **Stateless** — any instance can serve any request | Adding instances behind Nginx |
| **RabbitMQ** | Durable buffer between "accepted" and "done"; distributes work | Stateful (durable queues on disk) | Vertically; clustering + quorum queues for HA |
| **Workers (worker-1..2)** | Consume messages, claim tasks idempotently, do the work, record results | Stateless | Adding consumers (competing-consumer pattern) |
| **PostgreSQL** | Source of truth: users, tasks, results, logs | Stateful | Vertically; read replicas; RDS Multi-AZ |
| **Redis** | Cache-aside for task reads, shared rate-limit counters | Disposable (no persistence) | Vertically; ElastiCache |
| **Prometheus / Grafana** | Metrics collection, alert rules, dashboards | Stateful (TSDB) | Retention tuning; managed Prometheus |

### How it evolves

The tutorial builds this progressively. Each step exists because the previous one runs into a concrete limitation:

`single process` → `modular backend` → `PostgreSQL` → `Redis` → `RabbitMQ + worker` → `Docker` → `Nginx` → `3 APIs, 2 workers` → `one EC2` → `VPC / subnets / SGs` → `five EC2s` → `DNS + HTTPS` → `Prometheus + Grafana` → `GitHub Actions` → `load tests` → `failure tests`

---

## Technology stack

Every component answers the same questions: *what problem does it solve, what happens without it, what does it cost?*

| Technology | Problem it solves | Without it | Complexity it adds | Alternative |
|---|---|---|---|---|
| **FastAPI** | Typed request validation + OpenAPI with async I/O | Hand-written validation; blocking I/O per request | Async pitfalls (blocking the loop) | Django REST, Flask, Go |
| **PostgreSQL** | Durable, consistent, relational data with transactions | Data loss, race conditions on concurrent updates | Migrations, pooling, backups | MySQL, Aurora |
| **Redis** | Sub-millisecond shared reads; counters shared across API instances | Every read hits PostgreSQL; per-instance rate limits (3× the limit with 3 APIs) | Staleness, invalidation, a second failure mode | In-process cache (not shared), Memcached |
| **RabbitMQ** | Decouples accepting work from doing it; smooths bursts; survives worker crashes | Long HTTP requests, timeouts, lost work on crash | Duplicates, poison messages, a stateful broker to operate | SQS (managed), Kafka (log/streaming), Postgres `SKIP LOCKED` queue |
| **Nginx** | One public entry point; TLS; spreads load; hides internal topology | Clients must know every API server; no failover | Another config language and hop | AWS ALB, HAProxy, Caddy, Traefik |
| **Docker / Compose** | Identical artifact from laptop to EC2 | "Works on my machine", snowflake servers | Image hygiene, Docker networking quirks | systemd + venv, Kubernetes, ECS |
| **Prometheus / Grafana** | Numbers over time: rates, latency percentiles, saturation | Debugging from logs and guesses | Cardinality management, another stateful service | CloudWatch, Datadog, managed Prometheus |
| **GitHub Actions + ECR** | Repeatable build/test/deploy with an audit trail | Manual SSH deploys, drift, no rollback story | Pipeline maintenance, IAM trust setup | GitLab CI, CodePipeline; GHCR |
| **k6** | Measured capacity instead of assumed capacity | Discovering limits in production | Test realism matters | Locust, Gatling, wrk |

### Container registry: ECR over GHCR

Both work. **Amazon ECR** was chosen because it keeps the entire delivery path inside IAM with no stored credentials: GitHub Actions assumes a deploy role via **OIDC**, and EC2 instances pull images using their **instance role**. With GHCR, every EC2 host would need a GitHub token stored on disk. ECR also scans images on push, supports immutable tags, and keeps image pulls inside the AWS network (the S3 gateway endpoint carries layer downloads for free). GHCR's advantages — free for public images, no AWS setup, visible next to the code — matter less here than practicing least-privilege IAM.

---

## AWS infrastructure

### Components used

| AWS component | Role in this project | Why | Watch out for |
|---|---|---|---|
| **VPC** `10.20.0.0/16` | Private, isolated network for all instances | Internal services get addresses unreachable from the internet | CIDR overlap with networks you may connect later (VPN, peering) |
| **Subnets** | `public-a 10.20.1.0/24`, `private-a 10.20.11.0/24` (+ `-b` reserved in a 2nd AZ) | Public = route to Internet Gateway; private = no inbound path from the internet | "Public" is a routing property, not a checkbox |
| **Internet Gateway** | Internet access for the public subnet | Edge node must be reachable | Only useful together with a route and a public IP |
| **NAT instance** (t4g.nano) | Outbound-only internet for private instances (apt, SSM) | A NAT **Gateway** costs ~$33/month before data; a NAT instance ~$3 | Single point of failure for *outbound* traffic; disable source/dest check |
| **Route tables** | public: `0.0.0.0/0 → IGW`; private: `0.0.0.0/0 → NAT` | Defines which subnets are public | Forgetting to associate the subnet |
| **Security Groups** | One per role; rules reference other SGs | Stateful allow-lists at the hypervisor; self-maintaining as instances are added | Opening `0.0.0.0/0` on internal ports "just to test" |
| **EC2** | 5 Ubuntu 24.04 instances + NAT | Full control: learn Linux, Docker, networking | You operate patching, backups, recovery |
| **EBS** | Encrypted gp3 root volumes; separate 20 GiB data volume | Data survives instance replacement | `DeleteOnTermination=false` volumes keep billing after teardown |
| **Elastic IP** | Stable public IPv4 for the edge node | DNS must not break when the instance stops/starts | All public IPv4 addresses are billed hourly |
| **IAM** | EC2 instance role; GitHub OIDC deploy role | No long-lived access keys anywhere | Over-broad policies (`*:*`) |
| **SSM** | Parameter Store (secrets), Session Manager (shell), Run Command (deploys) | No inbound SSH needed for automation; audited access | Requires outbound HTTPS from instances (NAT) |
| **ECR** | Private image registry | IAM-authenticated pulls/pushes | Lifecycle policy, or storage grows forever |
| **DNS** | `A` record `api.example.com → EIP` | Human-readable, certificate-bound name | TTLs, propagation delays |

### Ports and exactly who may use them

| Port | Service | Allowed source | Why |
|---|---|---|---|
| 80 | Nginx HTTP | `0.0.0.0/0` | Let's Encrypt HTTP-01 validation + redirect to HTTPS |
| 443 | Nginx HTTPS | `0.0.0.0/0`, `::/0` | The only port real users need |
| 22 | SSH (edge) | **your IP /32** | Admin access; everything else via edge as jump host — or SSM, no SSH at all |
| 22 | SSH (private nodes) | `sg-edge` | Bastion pattern |
| 8000–8001 | FastAPI | `sg-edge` (Nginx), `sg-ops` (Prometheus) | Never public: Nginx is the only way in |
| 5432 | PostgreSQL | `sg-edge`, `sg-app`, `sg-broker`, `sg-ops` | API and worker hosts only |
| 6379 | Redis | same as PostgreSQL | Redis has weak auth; must never be internet-facing |
| 5672 | RabbitMQ AMQP | `sg-edge`, `sg-app`, `sg-ops` | Producers and consumers |
| 15672 | RabbitMQ UI | bound to `127.0.0.1` | Port-forward via SSH/SSM when needed |
| 3000 / 9090 | Grafana / Prometheus | bound to `127.0.0.1` | Port-forward; no public dashboards |
| 9100, 9101, 9121, 9187, 15692 | exporters / metrics | `sg-ops` | Metrics can leak internals; scrape privately |

### Three layers of network security

1. **Security Group** — AWS drops disallowed packets before they reach the instance. Primary control.
2. **ufw (host firewall)** — Linux kernel filtering inside the instance; defense in depth if an SG is misconfigured. *Caveat:* Docker-published ports bypass ufw, so internal containers publish on the **private IP only**, never `0.0.0.0`.
3. **Application** — JWT auth, per-user ownership checks, input validation, rate limiting, `pg_hba.conf` restricted to private ranges, Redis/RabbitMQ passwords.

### Single points of failure — stated plainly

This is **not** a highly available system, and it doesn't pretend to be:

| Component | If it fails | Production fix |
|---|---|---|
| edge node (Nginx + EIP) | Complete outage | ALB across 2 AZs + Auto Scaling Group |
| data node (PostgreSQL + Redis) | Complete outage for anything stateful | RDS Multi-AZ, ElastiCache with replica |
| broker node | Task submission fails (503); reads keep working | RabbitMQ 3-node cluster with quorum queues, or SQS |
| NAT instance | Private nodes lose outbound internet (deploys, updates); live traffic unaffected | NAT Gateway per AZ |
| Availability Zone | Everything is in one AZ | Spread every tier across ≥ 2 AZs |
| app node | Loses 2 of 3 APIs; api-1 carries all load | More nodes, across AZs |

---

## Request flows

### Synchronous request (`GET /api/v1/tasks/{id}`)

```
Client ──TLS──▶ Nginx (edge) ──round robin──▶ api-N
                                               │ 1. RequestContext middleware: request_id, timer
                                               │ 2. Rate limiter: Redis INCR ratelimit:ip:<ip>:<window>
                                               │ 3. JWT decode → load user (PostgreSQL)
                                               │ 4. TaskService.get → cache-aside (below)
                                               ▼
                         JSON ◀── 200 ◀── Nginx ◀── api-N    (X-Request-ID echoed end to end)
```

### Caching flow (cache-aside)

```
GET task ──▶ Redis GET task:v1:<id> ──hit──▶ ownership check ──▶ 200
                     │
                     └─miss─▶ SET lock:… NX PX 2000  (stampede protection)
                                  │
                                  ▼
                              PostgreSQL SELECT ──▶ Redis SET (TTL 60s + 0–10s jitter) ──▶ 200

Writes (process, delete, worker completion) commit to PostgreSQL first, then DEL the key.
Redis errors are logged, counted, and ignored: slower, never broken.
```

Cached: task detail (read-heavy, tolerates ≤ 60 s staleness). **Not** cached: task status (clients poll it to see *fresh* state), lists (invalidation across pages is costly), auth decisions.

### Asynchronous processing flow (`POST /api/v1/tasks/{id}/process`)

```
Client ─▶ api-N: UPDATE tasks SET status='queued' WHERE id=… AND status IN ('pending','failed')  ◀─ atomic
                 COMMIT
                 publish {task_id} → exchange "tasks" (persistent, publisher confirm)
         ◀─ 202 Accepted  Location: /api/v1/tasks/{id}/status

RabbitMQ "tasks.process" ─▶ worker-1 / worker-2  (prefetch = 4 each)
   worker: UPDATE … SET status='processing', lease_expires_at=now()+60s
           WHERE status='queued' OR (status='processing' AND lease expired)   ◀─ idempotency guard
           run work (asyncio for I/O, process pool for CPU)
           INSERT task_results … ON CONFLICT DO NOTHING; status='completed'; COMMIT
           ACK  ◀── only after commit
   transient error → publish to tasks.retry.N (5s / 30s / 120s TTL) → back to tasks.process
   permanent error or retries exhausted → status='failed', NACK → tasks.dead-letter
```

---

## CI/CD

```
push to main
   │
   ├─ quality      checkout → pip install → ruff → mypy → unit tests (+coverage)
   ├─ integration  service containers: PostgreSQL, Redis, RabbitMQ → alembic upgrade → end-to-end test
   ├─ build        docker buildx (GHA cache) → tag = 12-char git SHA → push to ECR   [OIDC, no keys]
   └─ deploy       environment "production" (optional manual approval)
                   SSM Run Command per role, sequentially:
                   migrate(app) → broker → ops → app (api-2, then api-3) → edge (api-1, nginx)
                   → public health check: /health/ready + every instance reports the new version
```

**On each host**, `remote-deploy.sh` logs in to ECR with the instance role, pulls the image, extracts the deployment bundle baked into the image (compose files, Nginx/Prometheus/service configs), renders secrets from Parameter Store, and runs `deploy.sh`, which updates containers one at a time, waits for health checks, and **automatically rolls back** to the previous release if verification fails.

**Rollback** is "deploy an older immutable tag": run the `rollback` workflow with a previous SHA. Code *and* configuration return to that version. Database migrations are not rolled back — they are written expand/contract so the previous code version keeps working.

---

## Observability

| Signal | Where | Used for |
|---|---|---|
| **Metrics** | Prometheus scrapes APIs, workers, RabbitMQ, PostgreSQL/Redis exporters, node-exporter | Rates, latency percentiles, saturation, alerting |
| **Logs** | JSON on stdout → Docker json-file (rotated) → `docker compose logs` / `journalctl` | Explaining *one* request: every line carries `request_id` from Nginx through API to worker |
| **Traces** | Not implemented (see improvements) | Per-hop latency breakdown across services |

Key metrics: `http_requests_total{route,status}`, `http_request_duration_seconds`, `cache_operations_total{result}`, `tasks_published_total`, `db_pool_connections_in_use`, `worker_messages_total{outcome}`, `worker_task_duration_seconds`, `rabbitmq_detailed_queue_messages_ready`, node CPU/memory/disk.

Dashboards (provisioned from code in [`monitoring/grafana/generate_dashboards.py`](monitoring/grafana/generate_dashboards.py)): **API Overview** (RED metrics per instance), **Workers & Queue** (depth, throughput, DLQ), **Infrastructure** (hosts, containers, PostgreSQL, Redis).

Alert rules ([`monitoring/prometheus/rules/alerts.yml`](monitoring/prometheus/rules/alerts.yml)) fire on symptoms: target down, error ratio > 5 %, p95 > 500 ms, backlog growing, no consumers, DLQ non-empty, disk/memory/CPU pressure.

*Monitoring* tells you **that** something known is wrong (predefined dashboards and alerts). *Observability* is the ability to answer **new** questions about why — which needs high-quality, correlated metrics, logs and traces.

---

## Failure handling

| Failure | What users see | Detection | Recovery | Data at risk |
|---|---|---|---|---|
| One API instance dies | Nothing for GETs (Nginx retries on next server); an in-flight POST may fail | Nginx passive check, `up{job="api"}==0` | Restart policy / redeploy | None (stateless) |
| All workers down | Submissions still `202`; tasks stay `queued` | `NoWorkersConsuming` alert, queue depth | Workers start and drain backlog | None — messages durable |
| Worker killed mid-task | Task finishes later | RabbitMQ redelivers unacked message | Lease expires (60 s), another worker re-claims | None; work may run twice (idempotent writes) |
| Redis down | Slightly slower; rate limiting off | `cache_operations_total{result="error"}`, readiness "degraded" | Restart; cache refills lazily | Cache only (by design) |
| RabbitMQ down | `POST /process` → 503, task marked `failed`; reads fine | readiness "degraded", `TargetDown` | Broker restart; clients retry | Unconfirmed publishes are reported as failures, never silently dropped |
| PostgreSQL down | 503/500 for almost everything | readiness 503, `TargetDown` | Restart; pool pre-ping reconnects | Committed data safe; `synchronous_commit=on` |
| Network latency to DB | Rising p95, then pool timeouts (5 s) | latency panels, pool gauge | Fix network; timeouts bound the damage | None |
| Poison message | Task `failed` | DLQ alert | Inspect DLQ, fix, resubmit | None |

Run them yourself: `scripts/chaos.sh list`.

---

## Security

- **SSH**: key-based only (ed25519), edge reachable from your IP /32, private nodes only via edge (ProxyJump). Prefer SSM Session Manager (no open port at all).
- **IAM least privilege**: EC2 role can pull *one* ECR repository and read *one* Parameter Store path. GitHub role can push that repository and send SSM commands only to instances tagged `Project=taskplatform`, and can be assumed only from `main` / the `production` environment.
- **No long-lived keys**: OIDC federation for CI; instance profiles for EC2. Access keys leak via laptops, logs and repos, and never expire on their own.
- **Secrets**: SSM Parameter Store SecureString (KMS); rendered per service into `0600` files at deploy time. Nothing secret in git, images, or compose files. Each container receives only its own secrets.
- **IMDSv2 with hop limit 1**: containers cannot reach the instance metadata service to steal role credentials.
- **TLS**: Let's Encrypt, TLS 1.2/1.3 only, HSTS, automatic renewal via systemd timer, HTTP → HTTPS redirect.
- **Application**: bcrypt password hashing (off the event loop), JWT with pinned algorithm and issuer, constant-time login responses, strict Pydantic schemas (`extra="forbid"`), ownership checks returning 404 (no ID enumeration), Redis-backed rate limiting + Nginx `limit_req`, explicit CORS allow-list, no stack traces in responses, non-root containers.
- **Data services**: never exposed publicly; `pg_hba.conf` allows only private ranges with SCRAM; dangerous Redis commands disabled.

---

## Local development

**Prerequisites:** Docker (Compose v2.20+), Python 3.11+ (for tests), `make`.

```bash
cp .env.example .env        # then replace every CHANGE_ME
make up                     # builds the image, runs migrations, starts ~16 containers
make ps                     # everything should be "healthy"

curl http://localhost:8080/health/ready
open http://localhost:8080/docs            # Swagger UI
open http://localhost:3000                 # Grafana (GRAFANA_ADMIN_* from .env)
open http://localhost:15672                # RabbitMQ management
```

Quick end-to-end check:

```bash
BASE=http://localhost:8080/api/v1
curl -s -X POST $BASE/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-password"}'
TOKEN=$(curl -s -X POST $BASE/auth/token -d 'username=me@example.com&password=a-long-password' | jq -r .access_token)
ID=$(curl -s -X POST $BASE/tasks -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"first","payload":{"kind":"sleep","duration_seconds":2}}' | jq -r .id)
curl -s -X POST $BASE/tasks/$ID/process -H "Authorization: Bearer $TOKEN"
sleep 3; curl -s $BASE/tasks/$ID/status -H "Authorization: Bearer $TOKEN" | jq
```

Tests and quality:

```bash
make install && source .venv/bin/activate
make lint              # ruff + mypy
make test              # unit tests, no infrastructure needed
make test-integration  # against compose PostgreSQL / Redis / RabbitMQ
```

---

## AWS deployment overview

Full console + CLI walkthrough in the tutorial. The short version:

```bash
# 0. Configure: region, GitHub repo, domain
$EDITOR infra/aws/cli/00-config.sh

# 1-4. Infrastructure (each script is commented line by line)
infra/aws/cli/01-network.sh           # VPC, subnets, IGW, route tables, S3 endpoint
infra/aws/cli/02-security-groups.sh   # one SG per role
infra/aws/cli/03-iam-ecr-secrets.sh   # roles, OIDC, ECR, Parameter Store
infra/aws/cli/04-instances.sh         # NAT instance, 5 nodes, data volume, Elastic IP

# 5. DNS: create  A  api.example.com → <edge Elastic IP>

# 6. Bootstrap each node once (Docker, AWS CLI, ufw, systemd, /data)
scp -r deploy tp-app:/tmp/ && ssh tp-app 'sudo /tmp/deploy/scripts/bootstrap-host.sh app /tmp/node.env'

# 7. Deploy: push to main (GitHub Actions) — or run .github/scripts/ssm-deploy.sh locally
#    Order the first time: data → broker → migrate → ops → app → edge

# 8. HTTPS
ssh tp-edge 'sudo /opt/taskplatform/current/deploy/scripts/issue-certificate.sh you@example.com'

# When finished learning — stop all charges:
infra/aws/cli/99-teardown.sh
```

---

## Environment variables

Local values live in `.env` (from [`.env.example`](.env.example)). On AWS, non-secret host settings live in `/opt/taskplatform/shared/node.env` ([example](deploy/node.env.example)) and secrets in Parameter Store under `/taskplatform/prod/`.

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `local` | `production` hides `/docs` |
| `INSTANCE_ID` | `local` | Name in logs/metrics (`api-2`, `worker-1`) |
| `JWT_SECRET_KEY` | — (required, ≥ 32 chars) | HMAC signing key for access tokens |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Token lifetime |
| `CORS_ALLOWED_ORIGINS` | empty | Comma-separated browser origins |
| `POSTGRES_HOST` / `PORT` / `DB` / `USER` / `PASSWORD` | `localhost` / `5432` / `taskplatform` / `taskplatform` / — | Database connection |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | Per-process pool; keep `(size+overflow) × processes < max_connections` |
| `DB_STATEMENT_TIMEOUT_MS` | `5000` | Kill runaway queries |
| `REDIS_HOST` / `PORT` / `PASSWORD` | `localhost` / `6379` / — | Cache + rate limiter |
| `CACHE_TASK_TTL_SECONDS` | `60` | Maximum staleness of cached task reads |
| `RATE_LIMIT_ENABLED` / `REQUESTS` / `WINDOW_SECONDS` | `true` / `100` / `60` | Per-client fixed window |
| `RABBITMQ_HOST` / `PORT` / `USER` / `PASSWORD` | `localhost` / `5672` / `taskplatform` / — | Broker |
| `WORKER_CONCURRENCY` | `4` | RabbitMQ prefetch = max in-flight tasks per worker |
| `WORKER_PROCESSING_LEASE_SECONDS` | `60` | Time before a crashed worker's task can be re-claimed |
| `WORKER_CPU_POOL_SIZE` | `2` | Processes for CPU-bound tasks |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted to set `X-Forwarded-For` |

---

## API endpoints

Base path `/api/v1`. Interactive docs at `/docs` (non-production). All task endpoints require `Authorization: Bearer <token>`.

| Method | Path | Success | Errors | Notes |
|---|---|---|---|---|
| `POST` | `/auth/register` | `201` user | `409` email taken, `422` | |
| `POST` | `/auth/token` | `200` token | `401` | OAuth2 password form |
| `GET` | `/auth/me` | `200` user | `401` | |
| `POST` | `/tasks` | `201` task + `Location` | `422`, `429` | `payload.kind`: `sleep` or `hash` |
| `GET` | `/tasks?limit=&cursor=&status=` | `200` `{items, next_cursor}` | `400` bad cursor | Keyset pagination |
| `GET` | `/tasks/{id}` | `200` task | `404` | Cached in Redis |
| `POST` | `/tasks/{id}/process` | `202` + `Location` status URL | `404`, `409` wrong state, `503` broker down | Asynchronous |
| `GET` | `/tasks/{id}/status` | `200` status (+ result when completed) | `404` | Never cached |
| `DELETE` | `/tasks/{id}` | `204` | `404` | Invalidates cache |
| `GET` | `/health/live` | `200` | | Process alive; includes version |
| `GET` | `/health/ready` | `200` ok/degraded | `503` DB down | Dependency checks |
| `GET` | `/metrics` | Prometheus text | | Blocked at Nginx |

Errors share one shape: `{"error": {"code": "...", "message": "...", "request_id": "...", "details": ...}}`.

---

## Load testing

k6 scenarios in [`loadtest/k6`](loadtest/k6): `smoke`, `normal` (constant arrival rate, 80 % reads), `high` (ramp to saturation), `burst` (10× spike and recovery), `task-processing` (submission rate vs worker throughput, end-to-end latency).

```bash
make load-normal
make load-tasks
scripts/scale-experiment.sh normal 150     # 1 vs 2 vs 3 API servers
```

What to measure: throughput (req/s), p50/p95/p99 latency, error rate, queue depth, worker throughput. How to read the scaling experiment:

- **Throughput rises with API count only while the API tier is the bottleneck.** Locally, all containers share your laptop's cores, so gains flatten quickly — the bottleneck moves to CPU contention or PostgreSQL. On EC2, each API node brings its own CPU.
- **p99 is the honest number.** A single slow instance in a round-robin pool raises tail latency for everyone.
- **API latency stays flat while queue depth grows** under `task-processing` when submissions exceed worker capacity — the queue absorbs the burst; end-to-end time pays for it.
- **t3 instances throttle** when CPU credits run out; sustained tests on AWS show a latency cliff that is an instance-type property, not a code regression.

---

## Repository structure

```
.
├── app/                        FastAPI application + worker (one codebase, one image)
│   ├── main.py                 app factory, lifespan (pools created once per process)
│   ├── api/                    HTTP layer: routes, dependency injection wiring
│   ├── core/                   config, logging, DB/Redis/RabbitMQ setup, security, metrics, errors
│   ├── middleware/             request ID propagation, access logs, HTTP metrics
│   ├── models/                 SQLAlchemy tables
│   ├── schemas/                Pydantic request/response/message contracts
│   ├── repositories/           SQL only
│   ├── services/               use cases: task orchestration, cache, rate limiter, publisher
│   ├── workers/                RabbitMQ consumer (transport) + handlers (the work)
│   └── utils/                  retry with exponential backoff + jitter
├── migrations/                 Alembic (run as a one-off job, never at API startup)
├── tests/                      unit (no infra) and integration (real services)
├── nginx/                      main config, proxy snippet, local + edge (HTTP/HTTPS) sites, upstream sets
├── config/                     PostgreSQL, Redis, RabbitMQ configuration files
├── monitoring/                 Prometheus (local + AWS targets, alert rules), Grafana provisioning + dashboards-as-code
├── deploy/
│   ├── compose/                one compose file per EC2 role (+ shared base)
│   ├── scripts/                bootstrap, render secrets, deploy/rollback, certificates
│   ├── systemd/                boot unit, certificate renewal timer
│   └── firewall/               ufw rules per role
├── infra/aws/                  IAM policy documents, AWS CLI provisioning + teardown scripts
├── loadtest/k6/                load test scenarios
├── scripts/                    chaos scenarios, scaling experiment
├── .github/                    CI/CD pipeline, rollback workflow, SSM deploy helper, Dependabot
├── docs/                       tutorial.html, architecture.html (+ tutorial sources)
├── Dockerfile                  multi-stage, non-root, healthcheck, embeds deployment bundle
├── docker-compose.yml          full local distributed stack (3 APIs, 2 workers, monitoring)
└── Makefile                    entry points for everything above
```

**Why layered?** Each layer has one reason to change. Routes change when the HTTP contract changes; repositories when SQL does; services when business rules do. The worker reuses repositories and the cache without importing anything HTTP-related, and tests replace any dependency through FastAPI's `dependency_overrides` instead of patching.

---

## Trade-offs

| Decision | Chosen | Given up |
|---|---|---|
| Self-managed PostgreSQL/Redis/RabbitMQ on EC2 | Learning, low cost, full visibility | Automated backups, failover, patching (RDS/ElastiCache/Amazon MQ) |
| Nginx on an instance instead of an ALB | TLS/LB config you can read line by line; cheaper | Multi-AZ, managed certificates (ACM), active health checks |
| NAT instance instead of NAT Gateway | ~$3 vs ~$33+/month | Managed availability and throughput |
| Fixed private IPs for discovery | Zero extra moving parts | Replacing an instance means keeping its IP (Route 53 private zone would fix this) |
| Commit-then-publish instead of transactional outbox | Simple, easy to follow | A crash between commit and publish leaves a task `queued` without a message |
| At-least-once + idempotent claim | Survives crashes without distributed transactions | Work may execute twice; handlers must tolerate it |
| Fixed-window rate limiting | One `INCR`, cheap | Up to 2× limit across a window boundary |
| Fail-open on Redis errors | Redis outage ≠ API outage | No rate limiting during a Redis outage |
| Compose per host instead of Kubernetes/ECS | Transparent, debuggable, cheap | Scheduling, self-healing across hosts, service discovery |

## Limitations

- Single AZ; several single points of failure (see table above).
- No automated PostgreSQL backups (add `pg_dump` to S3 or EBS snapshots via Data Lifecycle Manager).
- No transactional outbox and no "stuck `queued` task" reaper.
- No distributed tracing; no Alertmanager delivery (alerts visible only in Prometheus).
- Migrations are not automatically reversible during rollback.
- JWT access tokens cannot be revoked before expiry (no refresh-token/denylist flow).

## Future improvements

1. **Transactional outbox** + relay process; reaper for tasks stuck in `queued`/`processing`.
2. **RDS PostgreSQL Multi-AZ**, **ElastiCache**, and **Amazon MQ** or **SQS** — trade learning for operations.
3. **ALB + Auto Scaling Groups** in two AZs, ACM certificates, health-check-driven replacement.
4. **Terraform/OpenTofu** instead of CLI scripts: plan/diff, drift detection, reproducible teardown.
5. **OpenTelemetry tracing** (Tempo/Jaeger or X-Ray), **Loki** for centralized logs, **Alertmanager** to Slack/email.
6. **Route 53 private hosted zone** for service discovery; VPC interface endpoints for SSM/ECR to drop the NAT.
7. **Quorum queues** and publisher-side outbox for stronger delivery guarantees.
8. Container image signing (cosign) and vulnerability gating in CI; pin actions to commit SHAs.
9. Blue/green or canary deploys with automatic metric-based rollback.

---

## Lessons learned

- **Adding servers adds failure modes before it adds capacity.** Three APIs needed a load balancer, shared rate-limit state, trusted proxy headers, and health checks — before they delivered any speed-up.
- **Stateless tiers scale by copy-paste; stateful tiers are where the real engineering is.** Nearly every hard problem here lives in PostgreSQL, RabbitMQ or their interaction.
- **"Exactly once" is a property you build from at-least-once delivery plus idempotent writes**, not something a broker gives you.
- **Timeouts are a feature.** Every network call has one (pool 5 s, statement 5 s, Redis 0.5 s, publish 5 s, Nginx connect 3 s). Without them, one slow dependency becomes every request's problem.
- **Health checks can cause outages.** A readiness check that fails on Redis would take every API out of rotation at once.
- **Security is layered, and the layers have gaps** — the Docker/ufw interaction is the clearest example.
- **A queue hides overload instead of removing it.** Watch queue depth, not only API latency.
- **Measure, then scale.** The scaling experiment rarely shows linear gains; finding *why* is the lesson.

---

## Cost considerations

Prices are approximate on-demand USD for `ap-southeast-1`/`us-east-1` class regions; check the AWS Pricing Calculator for yours.

| Setup | Resources | ≈ per hour | ≈ per month (24/7) |
|---|---|---|---|
| **Minimal learning** | 1 × t3.small running the whole `docker-compose.yml`, 20 GB gp3, 1 public IPv4 | $0.03 | ~$20 |
| **This project (multi-EC2)** | 4 × t3.small, 1 × t3.medium, 1 × t4g.nano NAT, ~120 GB gp3, 2 public IPv4 | ~$0.15 | ~$115 |
| **Production-like** | ALB, 2 × ASG app nodes per AZ, RDS Multi-AZ db.t4g.medium, ElastiCache, Amazon MQ, NAT Gateway × 2 | ~$0.70+ | ~$500+ |

Expensive or surprising items:

- **NAT Gateway**: ~$0.045/hour **plus** ~$0.045/GB processed — often the largest line on a learning bill. This project uses a NAT instance.
- **Public IPv4**: every public IPv4 address (Elastic or not, attached or not) is billed ~$0.005/hour.
- **Data transfer**: out to the internet and **cross-AZ** traffic are billed per GB; same-AZ private traffic is free.
- **EBS**: billed while volumes exist, even when instances are stopped. The data volume survives termination by design.
- **t3 unlimited mode** charges for sustained CPU above baseline; instances here use `standard` credits (they throttle instead).
- **CloudWatch** detailed monitoring and log ingestion; Prometheus self-hosted avoids most of it.

Learning workflow: build → experiment → `infra/aws/cli/99-teardown.sh`. Stopping instances keeps EBS and IPv4 charges running; **terminate** when done. Set an **AWS Budget** alert (e.g. $20) before creating anything.

---

## License

MIT
