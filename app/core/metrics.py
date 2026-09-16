"""Prometheus metric definitions.

Label cardinality rule: labels must come from a small, bounded set. `route="/tasks/{task_id}"`
is fine; `path="/tasks/5f0c..."` would create one time series per task and eventually
take Prometheus down.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# --- HTTP (API) --------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
HTTP_IN_PROGRESS = Gauge("http_requests_in_progress", "HTTP requests currently in flight")

# --- Cache / rate limit ------------------------------------------------------------------
CACHE_OPERATIONS = Counter(
    "cache_operations_total", "Cache lookups by result", ["cache", "result"]  # hit|miss|error
)
RATE_LIMITED = Counter("rate_limited_requests_total", "Requests rejected by the rate limiter")

# --- Queue (API side) --------------------------------------------------------------------
TASKS_PUBLISHED = Counter("tasks_published_total", "Task messages published", ["result"])

# --- Database pool -----------------------------------------------------------------------
DB_POOL_CHECKED_OUT = Gauge("db_pool_connections_in_use", "DB connections lent out")
DB_POOL_SIZE = Gauge("db_pool_connections_open", "DB connections open in the pool")

# --- Worker ------------------------------------------------------------------------------
WORKER_MESSAGES = Counter(
    "worker_messages_total",
    "Messages handled by workers",
    ["outcome"],  # completed|retried|dead_lettered|skipped
)
WORKER_TASK_DURATION = Histogram(
    "worker_task_duration_seconds",
    "Time spent processing one task",
    ["kind"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
WORKER_IN_FLIGHT = Gauge("worker_tasks_in_flight", "Tasks currently being processed")
