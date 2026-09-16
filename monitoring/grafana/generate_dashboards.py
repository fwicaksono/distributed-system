"""Dashboards as code: generates the Grafana JSON files in ./dashboards.

    python monitoring/grafana/generate_dashboards.py

Editing JSON exported from the UI produces 2 000-line diffs nobody reviews. A small
generator keeps every panel's PromQL visible and reviewable in one place.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}


class Layout:
    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.row_h = 0
        self.next_id = 1

    def place(self, w: int, h: int) -> dict[str, int]:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def new_row(self) -> None:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0


def row(layout: Layout, title: str) -> dict:
    layout.new_row()
    panel = {"type": "row", "title": title, "id": layout.next_id, "collapsed": False,
             "gridPos": {"x": 0, "y": layout.y, "w": 24, "h": 1}, "panels": []}
    layout.y += 1
    layout.next_id += 1
    return panel


def timeseries(layout: Layout, title: str, targets: list[tuple[str, str]], *, unit: str = "short",
               w: int = 12, h: int = 8, description: str = "", stack: bool = False) -> dict:
    panel = {
        "type": "timeseries", "title": title, "description": description, "id": layout.next_id,
        "datasource": DS, "gridPos": layout.place(w, h),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"lineWidth": 2, "fillOpacity": 10 if not stack else 40,
                           "stacking": {"mode": "normal" if stack else "none"}},
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "calcs": ["mean", "max", "lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": expr, "legendFormat": legend}
                    for i, (expr, legend) in enumerate(targets)],
    }
    layout.next_id += 1
    return panel


def stat(layout: Layout, title: str, expr: str, *, unit: str = "short", w: int = 6, h: int = 4,
         thresholds: list[tuple[float | None, str]] | None = None, description: str = "") -> dict:
    steps = [{"value": v, "color": c} for v, c in (thresholds or [(None, "green")])]
    panel = {
        "type": "stat", "title": title, "description": description, "id": layout.next_id,
        "datasource": DS, "gridPos": layout.place(w, h),
        "fieldConfig": {"defaults": {"unit": unit,
                                     "thresholds": {"mode": "absolute", "steps": steps}},
                        "overrides": []},
        "options": {"colorMode": "background", "graphMode": "area", "reduceOptions":
                    {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "targets": [{"refId": "A", "datasource": DS, "expr": expr}],
    }
    layout.next_id += 1
    return panel


def dashboard(uid: str, title: str, panels: list[dict], tags: list[str]) -> dict:
    return {
        "uid": uid, "title": title, "tags": tags, "timezone": "browser", "schemaVersion": 39,
        "version": 1, "editable": False, "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "templating": {"list": []}, "annotations": {"list": []}, "panels": panels,
    }


def api_overview() -> dict:
    L = Layout()
    p = [
        row(L, "Golden signals (RED: Rate, Errors, Duration)"),
        stat(L, "Requests / s", 'sum(rate(http_requests_total{job="api"}[$__rate_interval]))',
             unit="reqps"),
        stat(L, "5xx error ratio",
             'sum(rate(http_requests_total{job="api",status=~"5.."}[$__rate_interval])) / '
             'clamp_min(sum(rate(http_requests_total{job="api"}[$__rate_interval])), 1e-9)',
             unit="percentunit", thresholds=[(None, "green"), (0.01, "orange"), (0.05, "red")]),
        stat(L, "p95 latency",
             'histogram_quantile(0.95, sum by (le) '
             '(rate(http_request_duration_seconds_bucket{job="api"}[$__rate_interval])))',
             unit="s", thresholds=[(None, "green"), (0.25, "orange"), (0.5, "red")]),
        stat(L, "API instances up", 'sum(up{job="api"})',
             thresholds=[(None, "red"), (2, "orange"), (3, "green")]),
        timeseries(L, "Requests / s per API instance",
                   [('sum by (instance_name) (rate(http_requests_total{job="api"}'
                     '[$__rate_interval]))', "{{instance_name}}")],
                   unit="reqps", stack=True,
                   description="Even split = round-robin load balancing is working."),
        timeseries(L, "Responses by status code",
                   [('sum by (status) (rate(http_requests_total{job="api"}[$__rate_interval]))',
                     "{{status}}")], unit="reqps", stack=True),
        timeseries(L, "Latency percentiles (all routes)",
                   [(f'histogram_quantile({q}, sum by (le) (rate('
                     f'http_request_duration_seconds_bucket{{job="api"}}[$__rate_interval])))',
                     f"p{int(q * 100)}") for q in (0.5, 0.95, 0.99)], unit="s"),
        timeseries(L, "p95 latency by route",
                   [('histogram_quantile(0.95, sum by (le, route, method) (rate('
                     'http_request_duration_seconds_bucket{job="api"}[$__rate_interval])))',
                     "{{method}} {{route}}")], unit="s"),
        row(L, "Dependencies"),
        timeseries(L, "Redis cache hit ratio",
                   [('sum(rate(cache_operations_total{result="hit"}[$__rate_interval])) / '
                     'clamp_min(sum(rate(cache_operations_total{result=~"hit|miss"}'
                     '[$__rate_interval])), 1e-9)', "hit ratio")],
                   unit="percentunit", w=8),
        timeseries(L, "Cache operations",
                   [('sum by (result) (rate(cache_operations_total[$__rate_interval]))',
                     "{{result}}")], unit="ops", w=8,
                   description="`error` > 0 means Redis is failing and requests fall through "
                               "to PostgreSQL."),
        timeseries(L, "DB pool connections in use",
                   [("db_pool_connections_in_use", "{{instance_name}} in use"),
                    ("db_pool_connections_open", "{{instance_name}} open")], w=8),
        timeseries(L, "Rate-limited requests / s",
                   [("sum(rate(rate_limited_requests_total[$__rate_interval]))", "429s")],
                   unit="reqps", w=8),
        timeseries(L, "Task publish results",
                   [('sum by (result) (rate(tasks_published_total[$__rate_interval]))',
                     "{{result}}")], unit="ops", w=8),
        timeseries(L, "In-flight requests",
                   [("sum by (instance_name) (http_requests_in_progress)", "{{instance_name}}")],
                   w=8),
    ]
    return dashboard("api-overview", "API Overview", p, ["api", "red"])


def workers_queue() -> dict:
    L = Layout()
    q = 'queue="tasks.process"'
    p = [
        row(L, "Queue"),
        stat(L, "Ready messages (backlog)",
             f"sum(rabbitmq_detailed_queue_messages_ready{{{q}}})",
             thresholds=[(None, "green"), (100, "orange"), (500, "red")]),
        stat(L, "Unacked (in progress)",
             f"sum(rabbitmq_detailed_queue_messages_unacked{{{q}}})"),
        stat(L, "Consumers", f"sum(rabbitmq_detailed_queue_consumers{{{q}}})",
             thresholds=[(None, "red"), (1, "orange"), (2, "green")]),
        stat(L, "Dead-lettered (DLQ)",
             'sum(rabbitmq_detailed_queue_messages_ready{queue="tasks.dead-letter"})',
             thresholds=[(None, "green"), (1, "red")]),
        timeseries(L, "Queue depth by queue",
                   [("sum by (queue) (rabbitmq_detailed_queue_messages_ready)", "{{queue}} ready"),
                    ("sum by (queue) (rabbitmq_detailed_queue_messages_unacked)",
                     "{{queue}} unacked")],
                   description="Growing `ready` = producers faster than consumers. Retry queues "
                               "fill up during dependency failures."),
        timeseries(L, "Publish vs deliver rate (broker-wide)",
                   [("sum(rate(rabbitmq_global_messages_received_total[$__rate_interval]))",
                     "published"),
                    ("sum(rate(rabbitmq_global_messages_acknowledged_total[$__rate_interval]))",
                     "acked")], unit="ops"),
        row(L, "Workers"),
        timeseries(L, "Throughput by outcome",
                   [('sum by (outcome) (rate(worker_messages_total[$__rate_interval]))',
                     "{{outcome}}")], unit="ops", stack=True),
        timeseries(L, "Completed / s per worker",
                   [('sum by (instance_name) (rate(worker_messages_total{outcome="completed"}'
                     '[$__rate_interval]))', "{{instance_name}}")], unit="ops", stack=True,
                   description="Competing consumers: both workers share the load."),
        timeseries(L, "Task duration p50 / p95",
                   [('histogram_quantile(0.5, sum by (le, kind) (rate('
                     'worker_task_duration_seconds_bucket[$__rate_interval])))', "p50 {{kind}}"),
                    ('histogram_quantile(0.95, sum by (le, kind) (rate('
                     'worker_task_duration_seconds_bucket[$__rate_interval])))', "p95 {{kind}}")],
                   unit="s"),
        timeseries(L, "In-flight tasks per worker (≤ prefetch)",
                   [("sum by (instance_name) (worker_tasks_in_flight)", "{{instance_name}}")]),
    ]
    return dashboard("workers-queue", "Workers & Queue", p, ["worker", "rabbitmq"])


def infrastructure() -> dict:
    L = Layout()
    p = [
        row(L, "Hosts (node-exporter)"),
        timeseries(L, "CPU usage",
                   [('1 - avg by (instance_name) (rate(node_cpu_seconds_total{mode="idle"}'
                     '[$__rate_interval]))', "{{instance_name}}")], unit="percentunit", w=8),
        timeseries(L, "Memory used",
                   [("1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes",
                     "{{instance_name}}")], unit="percentunit", w=8),
        timeseries(L, "Disk used (/)",
                   [('1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / '
                     'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
                     "{{instance_name}}")], unit="percentunit", w=8),
        timeseries(L, "Network received",
                   [('sum by (instance_name) (rate(node_network_receive_bytes_total'
                     '{device!~"lo|veth.*|docker.*|br-.*"}[$__rate_interval]))',
                     "{{instance_name}}")], unit="Bps", w=8),
        timeseries(L, "Load average (1m) per core",
                   [('node_load1 / count without (cpu, mode) '
                     '(node_cpu_seconds_total{mode="idle"})', "{{instance_name}}")], w=8),
        timeseries(L, "CPU steal / iowait",
                   [('avg by (instance_name) (rate(node_cpu_seconds_total{mode="steal"}'
                     '[$__rate_interval]))', "{{instance_name}} steal"),
                    ('avg by (instance_name) (rate(node_cpu_seconds_total{mode="iowait"}'
                     '[$__rate_interval]))', "{{instance_name}} iowait")],
                   unit="percentunit", w=8,
                   description="Steal > 0 on t3 instances = CPU credits exhausted."),
        row(L, "Containers (cAdvisor, local only)"),
        timeseries(L, "Container CPU",
                   [('sum by (name) (rate(container_cpu_usage_seconds_total{name=~".+"}'
                     '[$__rate_interval]))', "{{name}}")], unit="short"),
        timeseries(L, "Container memory",
                   [('sum by (name) (container_memory_working_set_bytes{name=~".+"})',
                     "{{name}}")], unit="bytes"),
        row(L, "PostgreSQL & Redis"),
        timeseries(L, "PostgreSQL connections by state",
                   [("sum by (state) (pg_stat_activity_count)", "{{state}}"),
                    ("max(pg_settings_max_connections)", "max_connections")], w=8),
        timeseries(L, "PostgreSQL transactions / s",
                   [("sum(rate(pg_stat_database_xact_commit[$__rate_interval]))", "commit"),
                    ("sum(rate(pg_stat_database_xact_rollback[$__rate_interval]))", "rollback")],
                   unit="ops", w=8),
        timeseries(L, "Redis memory",
                   [("redis_memory_used_bytes", "used"), ("redis_memory_max_bytes", "maxmemory")],
                   unit="bytes", w=8),
        timeseries(L, "Redis keyspace hits vs misses",
                   [("rate(redis_keyspace_hits_total[$__rate_interval])", "hits"),
                    ("rate(redis_keyspace_misses_total[$__rate_interval])", "misses")],
                   unit="ops", w=8),
        timeseries(L, "Redis evicted / expired keys",
                   [("rate(redis_evicted_keys_total[$__rate_interval])", "evicted"),
                    ("rate(redis_expired_keys_total[$__rate_interval])", "expired")],
                   unit="ops", w=8),
        timeseries(L, "Redis connected clients", [("redis_connected_clients", "clients")], w=8),
    ]
    return dashboard("infrastructure", "Infrastructure", p, ["infra"])


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for build in (api_overview, workers_queue, infrastructure):
        board = build()
        path = OUT / f"{board['uid']}.json"
        path.write_text(json.dumps(board, indent=2) + "\n")
        print(f"wrote {path} ({len(board['panels'])} panels)")


if __name__ == "__main__":
    main()
