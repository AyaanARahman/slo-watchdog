# slo-watchdog

A Kubernetes service with deliberately controllable failure modes, SLO-based
burn-rate alerting, and a harness that breaks the service on purpose to check the
alerts actually work.

That last part is the reason this repo exists. Alerting rules are code that runs in
production and basically never gets tested. They get written once, eyeballed on the
day, and then they rot. A typo in a label selector means the rule matches nothing and
pages nobody, and you find out during an incident.

So: four failure scenarios, and every one of them asserts both that the right alert
fired *and* that the unrelated ones stayed quiet. The negative assertion is the part
that took the most work to get right.

## What the harness prints

```
==============================================================================
  SCENARIO: slow-burn
==============================================================================
  [setup]  resetting chaos and deleting prior series ...
  [setup]  all owned alerts inactive
  [chaos]  {'error_rate': 0.012, 'latency_ms': 0, 'latency_jitter_ms': 0}
  [load ]  k6 at 60 rps for 180s (+45s grace)
  [fire ]  t=  31.1s  BudgetApiAvailability/ticket  <- expected
  [PASS ]  BudgetApiAvailability/ticket fired at t=31.1s
  [PASS ]  BudgetApiAvailability/page stayed silent
  [PASS ]  BudgetApiLatency/page stayed silent
  [PASS ]  BudgetApiLatency/ticket stayed silent
```

A 1.2% error rate opens a ticket and does not page. That's the scenario I care about
most: a naive `error_rate > 5%` rule either pages here or misses it entirely, and
somebody loses a night's sleep over something that could have waited until Tuesday.

Full results, including detection times for all four scenarios, are in
[docs/results.md](docs/results.md).

## Running it

Needs Docker with ~8GB, plus `kind`, `kubectl`, `helm`, `k6`, and `terraform`.

```bash
make venv          # python 3.12 venv, service installed editable
make tf-up         # terraform: kind cluster + kube-prometheus-stack, then deploy the app
make harness       # all four scenarios, pass/fail report
```

`make tf-up` takes about eight minutes cold, mostly pulling images. `make harness`
takes about twenty, because several scenarios have to wait for a five-minute window
to flush.

If you'd rather skip Terraform, `make local-up` does the same thing with kind and
helm directly.

| | |
|---|---|
| budget-api | http://localhost:30080 |
| Prometheus | http://localhost:30090 |
| Alertmanager | http://localhost:30093 |
| Grafana | http://localhost:30030 (admin/admin) |

## How it fits together

The service exposes `POST /admin/chaos`, which sets an error rate, a latency, and a
jitter at runtime. Prometheus scrapes RED metrics off it every 15s. Two SLOs are
defined in [slo/budget-api.slo.yaml](slo/budget-api.slo.yaml) and expanded by Sloth
into 30 recording rules and 4 multi-window burn-rate alerts. The harness drives the
chaos endpoint, generates load with k6, then queries the Prometheus and Alertmanager
APIs and asserts on what it finds.

The SLOs are availability (99.5%) and latency (99% under 250ms). Both targets, the
burn rates, and the histogram buckets are derived rather than copied, and the working
is in [docs/slo-rationale.md](docs/slo-rationale.md).

Scenarios are YAML, so adding a fifth failure mode is a file rather than a code
change:

```yaml
chaos:
  error_rate: 0.012
expect:
  fire:
    - {alert: BudgetApiAvailability, severity: ticket}
  silent:
    - {alert: BudgetApiAvailability, severity: page}
    - {alert: BudgetApiLatency, severity: page}
    - {alert: BudgetApiLatency, severity: ticket}
```

## One measurement I didn't expect

The latency SLI reads the `le="0.25"` histogram bucket directly instead of using
`histogram_quantile`. I knew the theory, but the size of the gap surprised me. With
latency injected uniformly over 40-200ms and 1801 requests through k6:

```
histogram_quantile(0.99, ...)  ->  248.0 ms
true p99 of the distribution   ->  198.4 ms
k6's own measured p99          ->  201.6 ms
bucket ratio at le=0.25        ->  0.9983   (exact)
```

The quantile was 25% high, and it's not noise. 37.5% of requests land under 100ms and
all of them under 250ms, so p99 falls inside the `(0.1, 0.25]` bucket and gets
interpolated: `0.1 + 0.15 * (0.99-0.375)/(1.0-0.375) = 247.6ms`. The data stopped at
200ms but the bucket runs to 250ms, so it reported a latency no request ever had.

The dashboard graphs both. Quantiles are fine for watching a trend; the SLO is
evaluated on the bucket count.

## Things I got wrong along the way

Worth writing down, because these were the interesting parts.

**Prometheus was scraping the pod twice.** Both the ClusterIP and NodePort Services
carried the same `app.kubernetes.io/name` label, and ServiceMonitors select on labels,
not on Service names. Any SLI query missing a `job` filter would have silently doubled
every count. Not crashed. Doubled.

**The harness was contaminating its own measurements.** `/admin/chaos` was being
counted in the SLI, so the setup calls for each scenario landed in the window that
scenario was about to assert over. Found by curling `/metrics` and reading it, not by
a test.

**Scenarios were leaking into each other.** Scenario 1 injects 40% errors; twenty-five
minutes later `rate1h` was still 0.2787, so scenario 2's "must not page" assertion
failed on scenario 1's data. That isn't a bug in the alerting, it's long windows doing
exactly their job. Each scenario now deletes its series through the Prometheus admin
API first, the same way an integration suite truncates its database.

**Terraform picked the Kubernetes version, and didn't say so.** `terraform apply` died
with `kubeadm init ... exit status 1`. The `tehcyx/kind` provider embeds kind v0.31,
whose newest node image is v1.35.0, but I'd pinned v1.37.0 (which the v0.33 CLI I had
been using defaults to quite happily). `strings` on the provider binary found it.
`kind.yaml` now pins the same image so both paths build the same cluster.

## Known limitations

- The latency histogram isn't labelled by status, so failed requests count toward the
  latency SLI. Mitigated by injecting latency before the error decision, so errors
  aren't artificially fast, but the real fix costs series cardinality.
- One replica, and chaos config lives in process memory. Two replicas means
  `POST /admin/chaos` hits one of them and the other keeps serving cleanly, which
  would make every assertion non-deterministic.
- `/admin/chaos` has no auth. Fine on a laptop, not fine anywhere else.
- Detection times in `docs/results.md` are lower bounds. `rate()` covers the samples
  that exist, not the window's nominal length, so on a young Prometheus every window
  collapses toward the same value. In steady state, 40% errors need about 11 minutes
  to pull a 1h average past the page threshold, not the 55s measured here.
- The base image carries 54 unfixed HIGH/CRITICAL CVEs from Debian. Trivy runs with
  `--ignore-unfixed` because there's no action to take on them; distroless would
  remove most, at the cost of having no shell to debug with.

## Layout

```
service/      FastAPI app, chaos injection, RED metrics, 82 tests
slo/          SLO definitions; slo/generated is Sloth output, don't edit
harness/      run.py, the k6 script, and scenarios/*.yaml
k8s/          plain manifests, applied with kubectl
terraform/    kind cluster + monitoring stack
monitoring/   helm values and the Grafana dashboard JSON
docs/         slo-rationale.md and results.md
```

Terraform owns the cluster and the monitoring platform. It does not own the
application, partly because infra changes weekly while workloads change hourly, and
partly because `kubernetes_manifest` resolves CRD schemas at plan time and the CRDs
are installed by the Helm release in the same config.

`make check` runs ruff, mypy strict, and pytest. CI adds a Trivy scan, a check that
`slo/generated` isn't stale, and `terraform validate`.
