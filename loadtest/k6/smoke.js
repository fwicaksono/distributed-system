// SMOKE: 1 user, 30 seconds. Run before any heavier test: proves the stack works at all.
//   docker compose --profile loadtest run --rm k6 run /scripts/smoke.js
//   BASE_URL=https://api.example.com k6 run loadtest/k6/smoke.js    (against AWS)
import { check, sleep } from 'k6';
import http from 'k6/http';
import {
  BASE_URL, createTask, createUsers, getTask, submitTask, summaryFor, taskStatus,
} from './lib.js';

export const options = {
  vus: 1,
  duration: '30s',
  thresholds: { checks: ['rate==1.0'] },
};

export function setup() {
  const ready = http.get(`${BASE_URL}/health/ready`);
  check(ready, { 'stack is ready': (r) => r.status === 200 });
  return { token: createUsers(1)[0] };
}

export default function ({ token }) {
  const id = createTask(token, { kind: 'sleep', duration_seconds: 0.5 });
  getTask(token, id);
  submitTask(token, id);
  sleep(3);
  check(taskStatus(token, id), { 'processed by a worker': (r) => r.json('status') === 'completed' });
}

export const handleSummary = summaryFor('smoke');
