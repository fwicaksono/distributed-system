// HIGH traffic: ramp the arrival rate up in stages to find the saturation point —
// the rate at which p95 latency bends upward and errors appear.
//   docker compose --profile loadtest run --rm k6 run /scripts/high.js
import {
  createTask, createUsers, getTask, listTasks, pickToken, summaryFor,
} from './lib.js';

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: 20,
      timeUnit: '1s',
      preAllocatedVUs: 100,
      maxVUs: 1000,
      stages: [
        { target: 100, duration: '1m' },
        { target: 200, duration: '1m' },
        { target: 400, duration: '1m' },
        { target: 600, duration: '1m' },
        { target: 0, duration: '30s' },
      ],
    },
  },
  // Looser thresholds: this test is EXPECTED to find the limit.
  thresholds: {
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(99)<2000'],
  },
};

export function setup() {
  const tokens = createUsers(10);
  const ids = tokens.map((t) => createTask(t));
  return { tokens, ids };
}

export default function ({ tokens, ids }) {
  const i = (__VU + __ITER) % tokens.length;
  const roll = Math.random();
  if (roll < 0.7) getTask(tokens[i], ids[i]);
  else if (roll < 0.9) listTasks(pickToken(tokens));
  else createTask(tokens[i]);
}

export const handleSummary = summaryFor('high');
