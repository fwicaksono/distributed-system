// BURST traffic: quiet -> sudden 10x spike -> quiet. Tests elasticity of fixed capacity:
// connection pools, Nginx limit_req burst, RabbitMQ buffering, and recovery afterwards.
//   docker compose --profile loadtest run --rm k6 run /scripts/burst.js
import {
  createTask, createUsers, getTask, pickToken, submitTask, summaryFor,
} from './lib.js';

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-arrival-rate',
      startRate: 20,
      timeUnit: '1s',
      preAllocatedVUs: 200,
      maxVUs: 1500,
      stages: [
        { target: 20, duration: '1m' },    // baseline
        { target: 500, duration: '10s' },  // spike in 10 seconds
        { target: 500, duration: '1m' },   // sustain
        { target: 20, duration: '10s' },   // drop
        { target: 20, duration: '2m' },    // recovery: does latency return to baseline?
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.05'],
  },
};

export function setup() {
  const tokens = createUsers(10);
  const ids = tokens.map((t) => createTask(t));
  return { tokens, ids };
}

export default function ({ tokens, ids }) {
  const i = (__VU + __ITER) % tokens.length;
  if (Math.random() < 0.85) {
    getTask(tokens[i], ids[i]);
  } else {
    const token = pickToken(tokens);
    const id = createTask(token);
    if (id) submitTask(token, id);
  }
}

export const handleSummary = summaryFor('burst');
