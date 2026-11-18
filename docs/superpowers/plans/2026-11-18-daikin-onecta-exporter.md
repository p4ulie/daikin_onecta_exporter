# Daikin Onecta exporter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only Prometheus exporter for a Daikin Altherma via the Onecta cloud API, with login helper, image, Helm chart, alerts and a "Heat pump" dashboard, deployed by homenet_2.0.

**Architecture:** Copy of `~/Work/Personal/taphome_exporter` at v0.1.2 (read `taphome_exporter/`, `helm/taphome-exporter/`, `tests/`, `Dockerfile`, `mise.toml`, `README.md`): a background thread polls and builds samples (a malformed reply is a failed poll), a custom collector formats the last state.

**Tech Stack:** Python 3.12, prometheus_client, urllib (stdlib), pytest, Helm 3, Grafana JSON.

**Spec:** `docs/superpowers/specs/2026-11-18-daikin-onecta-exporter-design.md`

## Global Constraints

- Read-only: GET `/v1/gateway-devices` and the token endpoint only. Never log, print or put in an exception message any token or client secret.
- Env names, defaults (interval 600 s, minimum 60, port 9879, token file `/data/token.json`), metric names, labels, alert names/exprs/`for` exactly as the spec.
- Public repo: fixtures synthetic, shaped like the spec's API section (no real names, serials, SSIDs, IPs, MACs). Do not copy `jwillemsen/daikin_onecta`'s fixture verbatim.
- Chart version == appVersion == image tag (start `0.1.0`); tests must not pin the number (they check the three agree). Image `10.34.1.1:30095/daikin-onecta-exporter`. No CRDs. Dashboard title without a folder prefix; tests assert it.
- Comments explain why, dry; `ponytail:` for deliberate shortcuts, as the TapHome repo.

## Review Focus

1. Refresh token rejected (400/401 `invalid_grant`) → `daikin_login_ok 0`, `daikin_up 0`, no tight retry loop, nothing secret logged.
2. HTTP 429 with `Retry-After` → no call until it has passed; rate-limit gauges updated from headers.
3. Token file missing → seeded from `DAIKIN_TOKEN_JSON`; present → env seed ignored; written atomically after each refresh; process restart keeps the rotated token.
4. Consumption buckets with `null` (future) and a management point without `consumptionData` → sums skip them, no scrape error.
5. `DAIKIN_INTERVAL` below 60 → 60; above-limit call rate (> 180/day) → one warning at start.

---

### Task 1: Exporter, login helper, image, repo scaffolding

**Files:** `daikin_onecta_exporter/{__init__,onecta,exporter,login}.py`, `tests/test_exporter.py`, `tests/test_onecta.py`, `tests/test_login.py`, `tests/fixtures/gateway-devices.json`, `requirements.txt`, `requirements-dev.txt` (pytest, pyyaml), `mise.toml` (`test`, `exporter`, `login`, `image:build`, `image:push` from `helm/daikin-onecta-exporter/Chart.yaml`), `Dockerfile` (writable `/data` volume expected, root FS read-only), `.dockerignore`, `.gitignore` (incl. `.superpowers/`), `.env.example`, `README.md`, `LICENSE` (MIT, Pavol Antalik, 2026).

**Interfaces:** `onecta.Client(client_id, client_secret, token_file, seed_json=None, opener=urllib.request.urlopen)` with `.get_devices() -> list`, `.rate_limit` (dict of the four header ints and `retry_after`), `.authorized_at`, raising `LoginError` on invalid_grant and `RateLimited(retry_after)` on 429; `exporter.poll_once(state, client)`, `exporter.build(devices) -> samples`, `exporter.DaikinCollector(state)`, `exporter.poll_interval() -> float`, `exporter.main()`; `login.main()`.

- [ ] Failing tests with a fake opener (no network): good poll → every spec metric with labels from the fixture; energy sums skip `null`; refresh when access token near expiry, token file rewritten atomically and keeps `authorized_at`; seed only when file missing; invalid_grant → login_ok 0; 429 → next poll skipped until Retry-After; malformed reply (`[]`, missing `managementPoints`) → failed poll; interval clamp and warning; no token or secret in `caplog.text` (make the fake raise with the token in its message to prove it); login helper prints only the seed JSON on stdout.
- [ ] Implement; `mise run test` green; `docker build` works.
- [ ] Commit `feat: Daikin Onecta exporter and login helper`.

### Task 2: Helm chart, alerts, dashboard

**Files:** `helm/daikin-onecta-exporter/{Chart.yaml,values.yaml,.helmignore,templates/{_helpers.tpl,deployment.yaml,service.yaml,pvc.yaml,servicemonitor.yaml,prometheusrule.yaml,dashboards.yaml},dashboards/daikin.json}`, `tests/test_chart.py`, README chart + release + owner-steps section.

- [ ] Failing chart tests modelled on TapHome's: required secret name; defaults render Deployment, Service, PVC; Recreate strategy; fsGroup 65534; env from secretKeyRef (`client-id`, `client-secret`, `token-json`); `enableServiceLinks: False`; `/data` mounted from the PVC; toggles → the spec's five alerts exactly; dashboard uid `daikin`, title "Heat pump", every `daikin_` metric used is one the exporter produces, legends carry `{{management_point}}` or `{{sensor}}`/`{{mode}}`.
- [ ] Implement; `helm lint` clean; tests green. Commit `feat: Helm chart, alerts and Heat pump dashboard`.

### Task 3: homenet_2.0 deployment (controller, after tag v0.1.0, image pushed, owner's OpenBao values)

As `kubernetes/apps/taphome-exporter/`; spec's homenet section; `mise run check`.
