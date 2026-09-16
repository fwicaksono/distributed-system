// NORMAL traffic: steady, realistic mix. Baseline for every comparison.
//   docker compose --profile loadtest run --rm k6 run /scripts/normal.js
//
// Open model (constant-arrival-rate): k6 starts N iterations per second REGARDLESS of how
// slow the system is. A closed model (fixed VUs looping) hides overload: when the server
// slows down, VUs send fewer requests and latency looks deceptively stable.
import { sleep } from 'k6';
import {
  createTask, createUsers, defaultThresholds, getTask, listTasks, pickToken, summaryFor,
} from './lib.js';

export const options = {
  scenarios: {
    normal: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE || 50),  // iterations per second
      timeUnit: '1s',
      duration: __ENV.DURATION || '3m',
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
  thresholds: defaultThresholds,
};

export function setup() {
  const tokens = createUsers(5);
  // Seed some tasks so reads hit existing rows (and exercise the Redis cache).
  // seeded[j] is owned by tokens[j % tokens.length] — users can only read their own tasks.
  const ids = [];
  for (let i = 0; i < 50; i++) ids.push(createTask(tokens[i % tokens.length]));
  return { tokens, ids };
}

export default function ({ tokens, ids }) {
  const token = pickToken(tokens);
  const roll = Math.random();
  // 80% reads, 20% writes — typical of most APIs.
  if (roll < 0.6) {
    const j = (__VU * 7 + __ITER) % ids.length;
    getTask(tokens[j % tokens.length], ids[j]);
  } else if (roll < 0.8) {
    listTasks(token);
  } else {
    createTask(token);
  }
  sleep(0.1);
}

export const handleSummary = summaryFor('normal');
