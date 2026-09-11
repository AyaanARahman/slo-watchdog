# Why these SLOs, these burn rates, and these buckets

Every number in `slo/budget-api.slo.yaml` is derived here. Nothing is copied from a
blog post without being checked against what this service actually does.

---

## 1. The SLO window: 30 days

A rolling 30-day window. Two reasons: it matches the cadence people actually review
reliability on, and it is long enough that a single bad afternoon doesn't consume the
whole budget while still being short enough that a bad month is visible.

**The more careful alternative is 28 days** — exactly four weeks. A 30-day window
contains a varying number of weekends as it slides, so the traffic mix changes shape
underneath the SLI for reasons that have nothing to do with reliability. 28 days holds
the weekday/weekend composition constant. 30 days is used here to line up with the
standard burn-rate table below; 28 would be the better choice for a service with
strong weekly seasonality.

---

## 2. Availability: 99.5%

### The objective

**99.5% of requests must not return 5xx.**

The budget is what makes this concrete:

| Objective | Error budget | Downtime equivalent per 30d |
|---|---|---|
| 99.0% | 1% | 7.2 hours |
| **99.5%** | **0.5%** | **3.6 hours** |
| 99.9% | 0.1% | 43.2 minutes |
| 99.95% | 0.05% | 21.6 minutes |

### Why not higher

Each additional nine costs roughly an order of magnitude more engineering. This service
runs as **a single replica, on a single-node cluster, with no HA, no PodDisruptionBudget,
and a rolling update that briefly drops in-flight requests**. Promising 99.9% — 43
minutes a month — would be a promise the architecture cannot keep.

That matters more than it sounds. **An SLO the team cannot meet is one the team learns
to ignore**, and an ignored SLO is worse than none: it produces alerts people silence,
which is how real incidents get missed.

### Why not lower

99% would be 7.2 hours a month. At that point injected chaos barely moves the needle
and the alerts almost never fire, so the harness would have nothing to prove. 99.5% is
tight enough that a few minutes of injected failure is visibly expensive.

### The number that matters operationally

A **sustained 0.5% error rate exactly exhausts the budget in 30 days.** That is burn
rate 1x, and it is the honest summary of the objective: run better than 0.5% errors on
average, or start the month in deficit.

---

## 3. Latency: 99% of requests under 250ms

Two separate numbers, derived separately.

### The threshold: 250ms

Grounded in human perception. Roughly: ~100ms feels instantaneous, ~1s is the limit for
uninterrupted flow of thought. An API serving a UI should leave room for network transit
and client rendering inside that second, so a 250ms server-side budget is defensible.

**And critically — 250ms had to be a histogram bucket boundary.** See §5.

### The target: 99%, not 99.5%

Latency is set looser than availability on purpose. **Latency has a fatter tail than
availability**: garbage collection pauses, cold caches, noisy neighbours, and TCP
retransmits all produce occasional slow requests that are not really failures. Holding
latency to the same 99.5% would burn budget continuously on events no user would call
an outage, and the alert would become background noise.

So: 1% of requests may exceed 250ms.

---

## 4. Burn rates — the derivation

### What burn rate means

**Burn rate is how fast you are consuming error budget, relative to the rate that would
exactly exhaust it over the SLO window.**

- 1x → on pace to use exactly 100% of the budget at day 30.
- 2x → exhausted in 15 days.
- 14.4x → exhausted in about 50 hours.

### The formula

Over an alerting window `W` within an SLO window `T`, burning at rate `B` consumes:

```
budget_consumed = B × (W / T)
```

Rearranged, to pick a burn rate from "I want to know when X% of my budget is gone":

```
B = budget_fraction × T / W
```

### Applying it — T = 30 days = 720 hours

| Alert on | Calculation | Burn rate | Severity |
|---|---|---|---|
| 2% of budget in 1h | 0.02 × 720 / 1 | **14.4x** | page |
| 5% of budget in 6h | 0.05 × 720 / 6 | **6x** | page |
| 10% of budget in 1d | 0.10 × 720 / 24 | **3x** | ticket |
| 10% of budget in 3d | 0.10 × 720 / 72 | **1x** | ticket |

The budget fractions are the Google SRE workbook values. The logic behind them: 2% in
an hour is fast enough to be a catastrophe and rare enough to justify waking someone;
10% over three days is a leak that deserves attention but not at 3am.

### Turning burn rates into PromQL thresholds

A burn rate is a multiplier on the error budget, so the actual error ratio to compare
against is `B × (1 - SLO)`:

**Availability (budget 0.005):**

| Burn | Threshold | Meaning |
|---|---|---|
| 14.4x | **7.2%** | 7.2% of requests failing, sustained |
| 6x | 3.0% | |
| 3x | 1.5% | |
| 1x | 0.5% | exactly the exhaust-on-time rate |

**Latency (budget 0.01):**

| Burn | Threshold |
|---|---|
| 14.4x | **14.4%** of requests slower than 250ms |
| 6x | 6.0% |
| 3x | 3.0% |
| 1x | 1.0% |

Same burn rates, different thresholds, because the budgets differ. This is why burn
rate — not raw error percentage — is the right unit: it means the same thing for both
SLOs.

---

## 5. Histogram buckets, and why 0.25 is not negotiable

Buckets: `[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5]`

The latency SLI asks: *what fraction of requests completed in under 250ms?* A Prometheus
histogram is a set of cumulative counters, so `http_request_duration_seconds_bucket{le="0.25"}`
is **literally the count of requests that took 250ms or less**. If 0.25 is a bucket
edge, the SLI is an exact division of two counters.

If it is not an edge, you must use `histogram_quantile()`, which **interpolates linearly
within the containing bucket** — it assumes observations are spread uniformly across
the bucket, which latency never is.

### This was measured, not assumed

Latency was injected uniform over [40ms, 200ms] and 1801 requests driven through:

| | value |
|---|---|
| `histogram_quantile(0.99, ...)` | **248.0 ms** |
| True p99 of the injected distribution | 198.4 ms |
| k6's measured p99, end to end | 201.6 ms |
| Exact bucket ratio at `le="0.25"` | **0.9983 — exact** |

The quantile overestimated by ~50ms, **25% high**, and the arithmetic predicts it:
the true fraction under 100ms is 0.375 and under 250ms is 1.0, so p99 lands inside the
`(0.1, 0.25]` bucket and interpolation gives

```
0.1 + (0.25 - 0.1) × (0.99 - 0.375) / (1.0 - 0.375) = 247.6 ms
```

Predicted 247.6ms, Prometheus reported 248.0ms. The real data stopped at 200ms, but the
bucket runs to 250ms, so interpolation reports a latency **no request ever experienced**.

The dashboard shows both: the quantile panel for shape and direction, the bucket ratio
for the SLO. Graph the quantile, evaluate on the ratio.

### Why only ten buckets

Every bucket is a separate time series per `(method, path)`. Ten buckets plus `+Inf` is
11 series per label combination. Buckets are individually cheap and multiplicatively
expensive — this is the memory-versus-accuracy dial, and 10 is enough to resolve a
healthy p50 while still separating 800ms from 5s.

### Why a histogram and not a summary

A **summary** computes quantiles client-side, per instance — and **quantiles cannot be
aggregated**. The p99 across three pods is not the mean of their three p99s, and there
is no way to recover a fleet-wide p99 from per-pod summaries. A histogram ships raw
bucket counts, counters can be summed, and the quantile is computed after aggregation.
In Kubernetes, where replica count is a deployment detail, that rules summaries out.

---

## 6. Why multi-window, multi-burn-rate

Three failure modes of naive alerting, and the piece that fixes each.

**1. A single threshold on a short window pages you for blips.** A 30-second spike
crosses 7.2% and wakes someone for nothing.
→ **The long window (1h) buys precision.** The badness has to be sustained.

**2. A long window alone will not clear.** Fix the incident and the alert keeps firing
for up to an hour, because the bad data is still inside the window. On-call learns the
alert is unreliable.
→ **The short window (5m) buys reset speed.** Both windows must breach, so the alert
drops as soon as the short one does. Sloth uses short = long / 12.

**3. One burn rate cannot express urgency.** A total outage and a slow leak look the
same.
→ **Multiple burn rates separate page from ticket.** 14.4x means wake someone; 1x means
open a ticket for Tuesday.

The one-line version: **long window for precision, short window for reset speed,
multiple rates to separate paging from ticketing.**

The scenario that proves #3 is the important one: a 1.5% error rate must open a ticket
and must **not** page. A naive threshold pages, and someone loses sleep over something
that could have waited.

---

## 7. Two properties that make the harness possible

**The SLOs are independent.** Availability is driven by `error_rate`; latency by
`latency_ms`. Injecting errors with `latency_ms: 0` degrades availability while the
latency SLI stays at 1.0000 — verified: with 40% errors injected, both
`BudgetApiAvailability` alerts fired while both `BudgetApiLatency` alerts stayed
`inactive`. Without that independence, no negative assertion would mean anything.

**Latency is injected before the error decision.** A failure therefore costs the same
wall-clock time as a success. If errors returned instantly, then during an availability
incident a large share of requests would complete in ~0ms and land in the latency
histogram — **the latency SLI would improve during an outage.**

---

## 8. Measured behaviour, and one important caveat

With 40% errors injected at 40 rps:

| | |
|---|---|
| Time to `BudgetApiAvailability` page firing | **16 seconds** |
| `BudgetApiLatency` during the same window | stayed `inactive` |
| `slo:sli_error:ratio_rate5m` at firing | 0.368 (vs 0.072 threshold) |

### The caveat: window pre-fill

16 seconds is **not** representative of steady state, and it is important to understand
why before quoting it.

`rate()` computes over the samples actually present in the window, not over the window's
nominal length. On a young Prometheus there is less data than the window, so **every
window collapses to the same value**. Measured directly after the incident:

```
rate5m  = nan       (no current traffic)
rate30m = 0.2787
rate1h  = 0.2787     <- identical
rate2h  = 0.2787     <- identical
rate6h  = 0.2787     <- identical
```

With a full hour of clean traffic already in the window, a 40% error rate would need
`0.072 × 60 / 0.40 = 10.8 minutes` to drag the 1h average above the page threshold. The
5m window would cross in 54 seconds, but the `AND` means the alert waits for the slower
of the two. **Steady-state detection is ~11 minutes, not 16 seconds.**

Consequence for the harness: either drive baseline clean load long enough to fill the
longest window it asserts on, or report detection times as lower bounds and say why.

### Resolution is bounded by the firing branch, not the shortest window

After chaos was reset, the page kept firing even with `rate5m = NaN`. The reason is that
the page alert is an **OR of two branches**, and the second one was still true:

```
(rate5m > 7.2%  AND rate1h > 7.2%)   <- false, rate5m is NaN
OR
(rate30m > 3%   AND rate6h > 3%)     <- TRUE at 0.2787
```

So "does the alert resolve promptly?" has a more precise answer than expected:
**it resolves when the short window of whichever branch is firing ages out.** For the
14.4x branch that is ~5 minutes; for the 6x branch it is ~30 minutes; for the 3x ticket
branch, ~2 hours.

That is correct behaviour, not a bug — a spike large enough to burn 5% of a monthly
budget *should* stay visible for longer than five minutes. But a harness asserting
"resolves promptly" has to assert against the right branch.

---

## 9. What this design does not do well

- **The latency SLI counts failed requests.** The histogram is not labelled by status,
  so 5xx responses land in it. Mitigated by injecting latency before the error decision
  (§7), but the clean fix is a `status` label, at the cost of multiplying series count.
  A deliberate trade-off.
- **`rate()` over 3 days is expensive** and, on a cluster younger than 3 days, not
  meaningful. Recording rules make the alert evaluation cheap, but the underlying data
  still has to exist.
- **One replica means one chaos config.** See the deployment manifest; scaling needs
  either shared chaos state or a harness that fans out per pod.
