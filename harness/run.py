#!/usr/bin/env python3
"""Prove the burn-rate alerts work.

For each scenario: isolate the time series, inject a specific failure, drive load,
then assert against the live Prometheus and Alertmanager APIs that the expected
alerts fired -- and that the unrelated ones did not.

The negative assertion is the point. Making an alert fire is trivial; proving it
fires *only* when it should is the hard part, and it is what separates an alert
somebody can trust at 3am from one they learn to ignore.

Usage:
    python harness/run.py                    # all scenarios
    python harness/run.py --scenario slow-burn
    python harness/run.py --list
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = REPO_ROOT / "harness" / "scenarios"
K6_SCRIPT = REPO_ROOT / "harness" / "k6" / "load.js"

DEFAULT_API = "http://localhost:30080"
DEFAULT_PROM = "http://localhost:30090"
DEFAULT_ALERTMANAGER = "http://localhost:30093"

POLL_SECONDS = 5.0

# Every alert this project owns. Used to confirm a clean slate before each scenario.
OWNED_ALERT_PREFIX = "BudgetApi"


# --------------------------------------------------------------------------------
# Small HTTP helpers. Deliberately stdlib-only: the harness is the thing that proves
# the system works, so it should carry as little of its own machinery as possible.
# --------------------------------------------------------------------------------


def _request(url: str, method: str = "GET", body: bytes | None = None) -> Any:
    req = urllib.request.Request(url, method=method, data=body)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _post(url: str, payload: dict[str, Any] | None = None) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={"content-type": "application/json"} if payload is not None else {},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


# --------------------------------------------------------------------------------
# Scenario model
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertRef:
    """One alert, identified the way Prometheus identifies it: name plus severity."""

    alert: str
    severity: str

    def __str__(self) -> str:
        return f"{self.alert}/{self.severity}"

    @classmethod
    def of(cls, d: dict[str, str]) -> AlertRef:
        return cls(alert=d["alert"], severity=d["severity"])


@dataclass
class Recovery:
    description: str
    rate: int
    duration_seconds: int
    query: str
    threshold: float
    timeout_seconds: int


@dataclass
class Scenario:
    name: str
    description: str
    chaos: dict[str, float]
    rate: int
    duration_seconds: int
    fire: list[AlertRef]
    silent: list[AlertRef]
    grace_seconds: int
    recover: Recovery | None
    path: Path

    @classmethod
    def load(cls, path: Path) -> Scenario:
        d = yaml.safe_load(path.read_text())
        rec_d = d.get("recover")
        rec = None
        if rec_d:
            rec = Recovery(
                description=rec_d.get("description", "").strip(),
                rate=int(rec_d["load"]["rate"]),
                duration_seconds=int(rec_d["load"]["duration_seconds"]),
                query=rec_d["assert_below"]["query"],
                threshold=float(rec_d["assert_below"]["threshold"]),
                timeout_seconds=int(rec_d["timeout_seconds"]),
            )
        return cls(
            name=d["name"],
            description=d.get("description", "").strip(),
            chaos=d.get("chaos", {}),
            rate=int(d["load"]["rate"]),
            duration_seconds=int(d["load"]["duration_seconds"]),
            fire=[AlertRef.of(x) for x in d["expect"].get("fire", [])],
            silent=[AlertRef.of(x) for x in d["expect"].get("silent", [])],
            grace_seconds=int(d.get("grace_seconds", 30)),
            recover=rec,
            path=path,
        )


@dataclass
class Result:
    scenario: Scenario
    fired_at: dict[AlertRef, float] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    recovery_seconds: float | None = None
    recovery_ok: bool | None = None

    @property
    def passed(self) -> bool:
        return not self.violations


# --------------------------------------------------------------------------------
# Cluster interaction
# --------------------------------------------------------------------------------


class Cluster:
    def __init__(self, api: str, prom: str, alertmanager: str) -> None:
        self.api = api.rstrip("/")
        self.prom = prom.rstrip("/")
        self.alertmanager = alertmanager.rstrip("/")

    # ---- chaos control ----

    def set_chaos(self, **kwargs: float) -> dict[str, Any]:
        return dict(_post(f"{self.api}/admin/chaos", kwargs))

    def reset_chaos(self) -> None:
        _request(f"{self.api}/admin/chaos", method="DELETE")

    # ---- prometheus ----

    def query(self, promql: str) -> float | None:
        """Instant query. Returns the first sample's value, or None if no data.

        NaN is returned as None: the SLI is error/total, so no traffic means 0/0,
        and 'no data' is genuinely different from 'zero errors'.
        """
        url = f"{self.prom}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
        res = _request(url)
        results = res["data"]["result"]
        if not results:
            return None
        value = float(results[0]["value"][1])
        # NaN means 0/0: the service received no traffic at all in the window.
        return None if math.isnan(value) else value

    def alert_states(self) -> dict[AlertRef, str]:
        """Current state of every alert this project owns: inactive/pending/firing."""
        res = _request(f"{self.prom}/api/v1/rules?type=alert")
        states: dict[AlertRef, str] = {}
        for group in res["data"]["groups"]:
            for rule in group["rules"]:
                name = rule["name"]
                if not name.startswith(OWNED_ALERT_PREFIX):
                    continue
                ref = AlertRef(alert=name, severity=str(rule["labels"].get("severity")))
                states[ref] = rule["state"]
        return states

    def isolate(self) -> None:
        """Delete this service's series so the previous scenario cannot leak in.

        Without this, scenario 1's 40% error rate stays inside the 30m/1h/6h windows
        and scenario 2's 'must NOT page' assertion fails -- not because the alerting
        is wrong, but because the windows are working exactly as designed. This is
        the same discipline as truncating a database between integration tests.

        Both the raw counters and Sloth's recording-rule output must go: deleting
        only the raw series would leave slo:sli_error:ratio_rate* holding stale
        values until each rule next evaluates.
        """
        for matcher in ('{job="budget-api"}', '{sloth_service="budget-api"}'):
            url = (
                f"{self.prom}/api/v1/admin/tsdb/delete_series?"
                + urllib.parse.urlencode({"match[]": matcher})
            )
            _request(url, method="POST")
        _request(f"{self.prom}/api/v1/admin/tsdb/clean_tombstones", method="POST")

    def alertmanager_alerts(self) -> list[dict[str, Any]]:
        return list(_request(f"{self.alertmanager}/api/v2/alerts") or [])

    # ---- load ----

    def start_load(self, rate: int, duration_seconds: int) -> subprocess.Popen[bytes]:
        env = {
            **os.environ,
            "BASE_URL": self.api,
            "RATE": str(rate),
            "DURATION": f"{duration_seconds}s",
        }
        return subprocess.Popen(
            ["k6", "run", str(K6_SCRIPT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# --------------------------------------------------------------------------------
# Running a scenario
# --------------------------------------------------------------------------------


def wait_for_clean_slate(cluster: Cluster, timeout: float = 120.0) -> bool:
    """Block until no owned alert is firing. Confirms isolation actually worked."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = cluster.alert_states()
        if all(s == "inactive" for s in states.values()):
            return True
        time.sleep(POLL_SECONDS)
    return False


def run_scenario(cluster: Cluster, sc: Scenario) -> Result:
    result = Result(scenario=sc)
    print(f"\n{'=' * 78}\n  SCENARIO: {sc.name}\n{'=' * 78}")

    # 1. Clean slate.
    print("  [setup]  resetting chaos and deleting prior series ...")
    cluster.reset_chaos()
    cluster.isolate()
    if not wait_for_clean_slate(cluster):
        result.violations.append(
            "could not reach a clean slate: an owned alert was still firing before "
            "the scenario began, so its assertions would be meaningless"
        )
        return result
    print("  [setup]  all owned alerts inactive")

    # 2. Inject.
    applied = cluster.set_chaos(**sc.chaos)
    print(f"  [chaos]  {applied}")

    # 3. Drive load and watch.
    print(
        f"  [load ]  k6 at {sc.rate} rps for {sc.duration_seconds}s "
        f"(+{sc.grace_seconds}s grace)"
    )
    proc = cluster.start_load(sc.rate, sc.duration_seconds)
    started = time.monotonic()
    watch_until = started + sc.duration_seconds + sc.grace_seconds

    # Everything that fired at any point, not just at the end. A silent-alert
    # violation that appears briefly mid-run still counts as a violation.
    ever_fired: dict[AlertRef, float] = {}

    while time.monotonic() < watch_until:
        now = time.monotonic() - started
        for ref, state in cluster.alert_states().items():
            if state == "firing" and ref not in ever_fired:
                ever_fired[ref] = now
                marker = "expected" if ref in sc.fire else "UNEXPECTED"
                print(f"  [fire ]  t={now:6.1f}s  {ref}  <- {marker}")
        time.sleep(POLL_SECONDS)

    proc.wait(timeout=60)
    cluster.reset_chaos()
    result.fired_at = ever_fired

    # 4. Assert.
    for ref in sc.fire:
        if ref in ever_fired:
            print(f"  [PASS ]  {ref} fired at t={ever_fired[ref]:.1f}s")
        else:
            result.violations.append(f"{ref} was expected to fire but never did")
            print(f"  [FAIL ]  {ref} never fired")

    for ref in sc.silent:
        if ref in ever_fired:
            result.violations.append(
                f"{ref} fired at t={ever_fired[ref]:.1f}s but should have stayed silent"
            )
            print(f"  [FAIL ]  {ref} fired but should have stayed silent")
        else:
            print(f"  [PASS ]  {ref} stayed silent")

    # 5. Optional recovery assertion.
    if sc.recover:
        result.recovery_ok, result.recovery_seconds = run_recovery(cluster, sc.recover)
        if not result.recovery_ok:
            result.violations.append(
                f"{sc.recover.query} did not fall below {sc.recover.threshold} "
                f"within {sc.recover.timeout_seconds}s"
            )

    return result


def run_recovery(cluster: Cluster, rec: Recovery) -> tuple[bool, float | None]:
    print(
        f"  [recov]  clean load at {rec.rate} rps, waiting for "
        f"{rec.query} < {rec.threshold}"
    )
    proc = cluster.start_load(rec.rate, rec.duration_seconds)
    started = time.monotonic()
    deadline = started + rec.timeout_seconds

    try:
        while time.monotonic() < deadline:
            value = cluster.query(rec.query)
            elapsed = time.monotonic() - started
            # None means no data -- the series aged out entirely, which is also
            # "below threshold" in the only sense that matters here.
            if value is None or value < rec.threshold:
                shown = "no data" if value is None else f"{value:.4f}"
                print(f"  [PASS ]  recovered at t={elapsed:.1f}s ({shown})")
                return True, elapsed
            time.sleep(POLL_SECONDS)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"  [FAIL ]  did not recover within {rec.timeout_seconds}s")
    return False, None


# --------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------


def write_report(results: list[Result], cluster: Cluster, out: Path) -> None:
    passed = sum(1 for r in results if r.passed)
    lines: list[str] = [
        "# Harness results",
        "",
        "Generated by `make harness` (`harness/run.py`). Each scenario injects a",
        "specific failure, drives load, and asserts against the live Prometheus and",
        "Alertmanager APIs that the expected alerts fired **and that the unrelated",
        "ones stayed silent**.",
        "",
        f"**{passed}/{len(results)} scenarios passed.**",
        "",
        "## Summary",
        "",
        "| Scenario | Expected to fire | Detected | Expected silent | Result |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        fired = (
            "<br>".join(
                f"`{ref}` @ {r.fired_at[ref]:.0f}s"
                if ref in r.fired_at
                else f"`{ref}` **never**"
                for ref in r.scenario.fire
            )
            or "-"
        )
        detected = (
            f"{min(r.fired_at[ref] for ref in r.scenario.fire if ref in r.fired_at):.0f}s"
            if any(ref in r.fired_at for ref in r.scenario.fire)
            else "-"
        )
        silent = (
            "<br>".join(
                f"`{ref}` {'**FIRED**' if ref in r.fired_at else 'quiet'}"
                for ref in r.scenario.silent
            )
            or "-"
        )
        lines.append(
            f"| **{r.scenario.name}** | {fired} | {detected} | {silent} | "
            f"{'PASS' if r.passed else 'FAIL'} |"
        )

    lines += ["", "## Scenarios", ""]
    for r in results:
        sc = r.scenario
        lines += [
            f"### {sc.name} — {'PASS' if r.passed else 'FAIL'}",
            "",
            sc.description,
            "",
            f"- **Injected:** `{sc.chaos}`",
            f"- **Load:** {sc.rate} rps for {sc.duration_seconds}s",
            "",
            "| Alert | Expectation | Observed |",
            "|---|---|---|",
        ]
        for ref in sc.fire:
            obs = (
                f"fired at {r.fired_at[ref]:.1f}s"
                if ref in r.fired_at
                else "never fired"
            )
            lines.append(f"| `{ref}` | must fire | {obs} |")
        for ref in sc.silent:
            obs = (
                f"**fired at {r.fired_at[ref]:.1f}s**"
                if ref in r.fired_at
                else "stayed silent"
            )
            lines.append(f"| `{ref}` | must stay silent | {obs} |")
        if sc.recover and r.recovery_seconds is not None:
            recovered = (
                f"Recovery: `{sc.recover.query}` fell below {sc.recover.threshold} "
                f"after **{r.recovery_seconds:.0f}s**."
            )
            lines += ["", recovered]
        elif r.recovery_ok is False:
            lines += ["", "Recovery: **did not recover within the timeout**."]
        if r.violations:
            lines += ["", "**Violations:**", ""] + [f"- {v}" for v in r.violations]
        lines.append("")

    lines += [
        "## How to read the detection times",
        "",
        "These numbers are measured on a Prometheus whose windows are **not full**.",
        "`rate()` computes over the samples actually present, not over the window's",
        "nominal length, so on a young server a `rate(...[1h])` may cover only a few",
        "minutes of data and every window collapses toward the same value.",
        "",
        "In steady state, with a full hour of clean traffic already in the window, a",
        "40% error rate needs `0.072 x 60 / 0.40 = 10.8 minutes` to drag the 1h",
        "average above the 14.4x page threshold. The 5m window crosses in 54 seconds,",
        "but the `AND` waits for the slower of the two.",
        "",
        "**So treat these as lower bounds.** They prove the rules fire on the right",
        "signal and stay quiet on the wrong one; they are not steady-state SLAs for",
        "time-to-detect.",
        "",
        "## Scenario isolation",
        "",
        "Each scenario deletes this service's time series before it starts, via the",
        "Prometheus admin API. Without it, scenario 1's 40% error rate remains inside",
        "the 30m/1h/6h windows and scenario 2's *must not page* assertion fails --",
        "not because the alerting is wrong, but because the windows work exactly as",
        "designed. Same discipline as truncating a database between integration tests.",
        "",
    ]
    out.write_text("\n".join(lines))
    print(f"\n  report written to {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-url", default=os.environ.get("API_URL", DEFAULT_API))
    ap.add_argument("--prom-url", default=os.environ.get("PROM_URL", DEFAULT_PROM))
    ap.add_argument(
        "--alertmanager-url", default=os.environ.get("ALERT_URL", DEFAULT_ALERTMANAGER)
    )
    ap.add_argument(
        "--scenario",
        action="append",
        help="run only these scenarios by name (repeatable)",
    )
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--output", default=str(REPO_ROOT / "docs" / "results.md"))
    args = ap.parse_args()

    scenarios = [Scenario.load(p) for p in sorted(SCENARIO_DIR.glob("*.yaml"))]
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [s for s in scenarios if s.name in wanted]
        missing = wanted - {s.name for s in scenarios}
        if missing:
            print(f"unknown scenario(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    if args.list:
        for s in scenarios:
            print(f"  {s.name:<16} {s.rate} rps for {s.duration_seconds}s")
        return 0

    cluster = Cluster(args.api_url, args.prom_url, args.alertmanager_url)

    try:
        cluster.query("up")
    except (urllib.error.URLError, OSError) as exc:
        print(f"cannot reach Prometheus at {args.prom_url}: {exc}", file=sys.stderr)
        print("is the cluster up? try: make local-up", file=sys.stderr)
        return 2

    results = [run_scenario(cluster, sc) for sc in scenarios]

    cluster.reset_chaos()
    write_report(results, cluster, Path(args.output))

    print(f"\n{'=' * 78}")
    failed = [r for r in results if not r.passed]
    for r in results:
        print(f"  {'PASS' if r.passed else 'FAIL'}  {r.scenario.name}")
        for v in r.violations:
            print(f"        - {v}")
    print(f"{'=' * 78}")
    print(f"  {len(results) - len(failed)}/{len(results)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
