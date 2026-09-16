// Shared helpers for all k6 scenarios.
import http from 'k6/http';
import { check, fail } from 'k6';
import { Counter, Trend } from 'k6/metrics';

export const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
export const API = `${BASE_URL}/api/v1`;

export const tasksCreated = new Counter('tasks_created');
export const tasksCompleted = new Counter('tasks_completed');
export const endToEnd = new Trend('task_end_to_end_seconds', true);

const JSON_HEADERS = { 'Content-Type': 'application/json' };

// Common thresholds: the test FAILS (non-zero exit) when these are violated, so load tests
// can gate a CI pipeline, not just produce pretty numbers.
export const defaultThresholds = {
  http_req_failed: ['rate<0.01'],                         // < 1% errors
  'http_req_duration{kind:read}': ['p(95)<250', 'p(99)<500'],
  'http_req_duration{kind:write}': ['p(95)<500'],
};

// Register a few users in setup() and share their tokens with all virtual users.
export function createUsers(count = 5) {
  const tokens = [];
  const runId = Math.random().toString(36).slice(2, 8);
  for (let i = 0; i < count; i++) {
    const email = `load-${runId}-${i}@example.com`;
    const password = 'load-test-password';
    const reg = http.post(`${API}/auth/register`, JSON.stringify({ email, password }), {
      headers: JSON_HEADERS, tags: { kind: 'setup' },
    });
    if (reg.status !== 201) fail(`register failed: ${reg.status} ${reg.body}`);
    const login = http.post(`${API}/auth/token`, { username: email, password }, { tags: { kind: 'setup' } });
    if (login.status !== 200) fail(`login failed: ${login.status} ${login.body}`);
    tokens.push(login.json('access_token'));
  }
  return tokens;
}

export function authHeaders(token) {
  return { headers: { ...JSON_HEADERS, Authorization: `Bearer ${token}` } };
}

export function pickToken(tokens) {
  return tokens[(__VU + __ITER) % tokens.length];
}

export function createTask(token, payload = { kind: 'sleep', duration_seconds: 1 }) {
  const res = http.post(
    `${API}/tasks`,
    JSON.stringify({ title: `load ${__VU}-${__ITER}`, priority: 5, payload }),
    { ...authHeaders(token), tags: { kind: 'write', name: 'POST /tasks' } },
  );
  check(res, { 'task created (201)': (r) => r.status === 201 });
  if (res.status === 201) tasksCreated.add(1);
  return res.status === 201 ? res.json('id') : null;
}

export function getTask(token, id) {
  const res = http.get(`${API}/tasks/${id}`, {
    ...authHeaders(token), tags: { kind: 'read', name: 'GET /tasks/{id}' },
  });
  check(res, { 'task fetched (200)': (r) => r.status === 200 });
  return res;
}

export function listTasks(token) {
  const res = http.get(`${API}/tasks?limit=20`, {
    ...authHeaders(token), tags: { kind: 'read', name: 'GET /tasks' },
  });
  check(res, { 'tasks listed (200)': (r) => r.status === 200 });
  return res;
}

export function submitTask(token, id) {
  const res = http.post(`${API}/tasks/${id}/process`, null, {
    ...authHeaders(token), tags: { kind: 'write', name: 'POST /tasks/{id}/process' },
  });
  check(res, { 'task accepted (202)': (r) => r.status === 202 });
  return res;
}

export function taskStatus(token, id) {
  return http.get(`${API}/tasks/${id}/status`, {
    ...authHeaders(token), tags: { kind: 'read', name: 'GET /tasks/{id}/status' },
  });
}

// Writes a JSON + text summary to /results so runs can be compared (1 vs 2 vs 3 APIs).
export function summaryFor(name) {
  return (data) => {
    const d = data.metrics.http_req_duration?.values || {};
    const reqs = data.metrics.http_reqs?.values || {};
    const failed = data.metrics.http_req_failed?.values || {};
    const line = [
      `scenario=${name}`,
      `apis=${__ENV.API_COUNT || '?'}`,
      `rps=${(reqs.rate || 0).toFixed(1)}`,
      `p50=${(d.med || 0).toFixed(1)}ms`,
      `p95=${(d['p(95)'] || 0).toFixed(1)}ms`,
      `p99=${(d['p(99)'] || 0).toFixed(1)}ms`,
      `errors=${((failed.rate || 0) * 100).toFixed(2)}%`,
    ].join(' ');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    return {
      stdout: `\n${line}\n`,
      [`/results/${name}-${__ENV.API_COUNT || 'x'}api-${stamp}.json`]: JSON.stringify(data, null, 2),
    };
  };
}
