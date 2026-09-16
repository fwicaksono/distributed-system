// TASK SUBMISSION + PROCESSING: measures the asynchronous pipeline end to end.
//
//   API accepts quickly (202)  vs  workers drain the queue at their own pace.
//
// Watch Grafana "Workers & Queue" during the run: if submissions/s > worker throughput,
// queue depth grows linearly and end-to-end time grows with it, while API latency stays
// flat. That is the whole point of a queue — and also its danger (unbounded backlog).
//
//   docker compose --profile loadtest run --rm k6 run /scripts/task-processing.js
//   RATE=40 KIND=hash docker compose --profile loadtest run --rm -e RATE -e KIND k6 run /scripts/task-processing.js
import { check, sleep } from 'k6';
import {
  createTask, createUsers, endToEnd, pickToken, submitTask, summaryFor, taskStatus,
  tasksCompleted,
} from './lib.js';

const KIND = __ENV.KIND || 'sleep';
const payload = KIND === 'hash'
  ? { kind: 'hash', iterations: 300000 }
  : { kind: 'sleep', duration_seconds: Number(__ENV.TASK_SECONDS || 1), failure_rate: Number(__ENV.FAILURE_RATE || 0) };

export const options = {
  scenarios: {
    submit: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE || 20),
      timeUnit: '1s',
      duration: __ENV.DURATION || '2m',
      preAllocatedVUs: 50,
      maxVUs: 400,
      exec: 'submit',
    },
    observe: {
      // A few VUs follow individual tasks to completion to measure end-to-end latency.
      executor: 'constant-vus',
      vus: 5,
      duration: __ENV.DURATION || '2m',
      exec: 'followOne',
    },
  },
  thresholds: {
    'http_req_duration{name:POST /tasks/{id}/process}': ['p(95)<300'],
    http_req_failed: ['rate<0.01'],
  },
};

export function setup() {
  return { tokens: createUsers(5) };
}

export function submit({ tokens }) {
  const token = pickToken(tokens);
  const id = createTask(token, payload);
  if (id) submitTask(token, id);
}

export function followOne({ tokens }) {
  const token = pickToken(tokens);
  const id = createTask(token, payload);
  if (!id) return;
  const started = Date.now();
  const accepted = submitTask(token, id);
  if (accepted.status !== 202) return;
  for (let i = 0; i < 300; i++) {
    const res = taskStatus(token, id);
    const status = res.status === 200 ? res.json('status') : null;
    if (status === 'completed' || status === 'failed') {
      check(res, { 'task completed': () => status === 'completed' });
      if (status === 'completed') {
        tasksCompleted.add(1);
        endToEnd.add((Date.now() - started) / 1000);
      }
      return;
    }
    sleep(1);
  }
}

export const handleSummary = summaryFor(`task-processing-${KIND}`);
