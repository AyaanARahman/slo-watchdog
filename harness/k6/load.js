// Constant-rate load generator for budget-api.
//
// Parameterised by environment so the harness can reuse it across scenarios:
//   BASE_URL  (default http://localhost:30080)
//   RATE      requests per second (default 50)
//   DURATION  e.g. "30s", "5m" (default 30s)

import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:30080";

export const options = {
  scenarios: {
    constant_load: {
      // Open model: issue RATE requests per second regardless of how slow the
      // service becomes. A closed model (fixed VUs looping) would *reduce* offered
      // load as latency rises, which would mask exactly the degradation we inject.
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 50),
      timeUnit: "1s",
      duration: __ENV.DURATION || "30s",
      preAllocatedVUs: 30,
      maxVUs: 200,
    },
  },

  // Deliberately no thresholds.
  //
  // k6 exits non-zero when a threshold is breached, and this project injects errors
  // on purpose — a threshold on http_req_failed would make k6 "fail" precisely when
  // a scenario is working as designed. Correctness is asserted against Prometheus
  // and Alertmanager, not against the load generator.
  thresholds: {},

  discardResponseBodies: true,
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

export default function () {
  const res = http.get(`${BASE}/api/v1/items`);
  // 500s are expected under injected chaos; anything else means a real problem.
  check(res, {
    "status is 200 or 500": (r) => r.status === 200 || r.status === 500,
  });
}
