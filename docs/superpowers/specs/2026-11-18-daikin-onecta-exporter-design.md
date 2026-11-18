# Daikin Onecta exporter — design

Date: 2026-11-18. Repo: `p4ulie/daikin_onecta_exporter` (public). Consumer: `p4ulie/homenet_2.0` (RKE2, ArgoCD).
Approved by the owner 2026-11-18 (Homenet_2.0 card 039). Template: `p4ulie/taphome_exporter`, whose shape this copies
(background poll, scrape serves the last result, Helm chart with ServiceMonitor, PrometheusRule, dashboard ConfigMap).

## Intent

Record what the Daikin Altherma 3 H heat pump (indoor `EHVX08S23EJ9W`, adapter `BRP069A78`) reports, without extra
hardware. The adapter has no local API, so the source is Daikin's Onecta cloud API. Read-only.

## Goals / non-goals / anti-goals

- Goals: exporter, image, chart with a "Heat pump" dashboard and alerts, a login helper for the one-time OAuth
  authorization, homenet deployment.
- Non-goals: control (no PATCH/POST to the API); compressor, flow, COP (not in Onecta; plan B is an ESP32 on X10A,
  card 038); CI image build.
- Anti-goals: exceeding Daikin's daily call limit; a scrape that calls the cloud; printing or logging any token;
  personal data (names, serials, SSIDs, IPs, MACs) from a real account in the public repo — fixtures are synthetic.

## Approaches

Decided by the owner: cloud (Onecta) first, X10A hardware as plan B. Local adapter API: dead on BRP069A78 (404).

## Onecta API (from `daikin-onecta` 0.6.2 and `jwillemsen/daikin_onecta`)

- OAuth2 authorization code: authorize `https://idp.onecta.daikineurope.com/v1/oidc/authorize`, token
  `https://idp.onecta.daikineurope.com/v1/oidc/token`, scopes `openid onecta:basic.integration offline_access`.
  Access token ~1 h; refresh token rotates on refresh; a login lasts about a year.
- Data: `GET https://api.onecta.daikineurope.com/v1/gateway-devices` (Bearer) returns every device with
  `managementPoints[]` (`embeddedId`, `managementPointType`, characteristics as `{"value": ...}`).
- Limits: headers `X-RateLimit-Limit-day`, `X-RateLimit-Remaining-day`, `X-RateLimit-Limit-minute`,
  `X-RateLimit-Remaining-minute`; HTTP 429 with `Retry-After`. About 200 calls/day.
- For this unit: `climateControlMainZone` has `sensoryData` (`roomTemperature`, `outdoorTemperature`,
  `leavingWaterTemperature`), `temperatureControl.operationModes.<mode>.setpoints.<name>`, `onOffMode`,
  `operationMode`, `errorCode`, `isInErrorState`, `isInWarningState`, `isInEmergencyState`,
  `consumptionData.electrical.<heating|cooling>.d` (24 two-hour buckets of whole kWh: 12 yesterday, then 12 today,
  `null` for the future); `domesticHotWaterTank` the same with `tankTemperature`, `isPowerfulModeActive`.
  Shape reference: `jwillemsen/daikin_onecta` `tests/fixtures/altherma.json` (same unit and adapter).

## Exporter

- Package `daikin_onecta_exporter/`: `onecta.py` (token refresh, GET, rate-limit headers; `urllib`, no new HTTP
  dependency), `exporter.py` (state, poll, collector, `main`), `login.py` (helper below).
- Env: `DAIKIN_CLIENT_ID`, `DAIKIN_CLIENT_SECRET` (required), `DAIKIN_TOKEN_FILE` (default `/data/token.json`),
  `DAIKIN_TOKEN_JSON` (seed, used only when the token file does not exist), **`DAIKIN_INTERVAL` (seconds, default
  600, minimum 60)**: log a warning at start when 86400/interval exceeds 180 calls/day. `DAIKIN_EXPORTER_PORT`
  (default 9879).
- Token file: `{"refresh_token", "access_token", "expires_at", "authorized_at"}`, written atomically (temp file +
  rename) after every refresh; `authorized_at` is carried over from the seed. Refresh when the access token has
  < 5 min left. A refresh failing with 400/401 (`invalid_grant`) is a login failure: `daikin_login_ok 0`.
- On 429: skip polls until `Retry-After` has passed.
- Metrics (labels `device` = gateway-device id, `management_point` = `embeddedId`):
  - `daikin_up`, `daikin_last_success_timestamp_seconds`, `daikin_read_duration_seconds`, `daikin_read_errors_total`,
    `daikin_login_ok`, `daikin_authorized_timestamp_seconds`, `daikin_ratelimit_remaining_day`,
    `daikin_ratelimit_remaining_minute`, `daikin_poll_interval_seconds`.
  - `daikin_temperature_celsius{sensor}` (every numeric `sensoryData` entry).
  - `daikin_setpoint_celsius{operation_mode,setpoint}`.
  - `daikin_on` (onOffMode on=1), `daikin_operation_mode_info{mode}` 1, `daikin_powerful_mode_active`.
  - `daikin_error_state`, `daikin_warning_state`, `daikin_emergency_state` (bool 0/1),
    `daikin_error_code_info{code}` 1 (raw code as the API sends it).
  - `daikin_energy_today_kwh{mode}`, `daikin_energy_yesterday_kwh{mode}` (sums of the non-null buckets).
  - `daikin_info{model, firmware}` per management point where present (indoor model, gateway firmware); never
    serials, MACs, SSIDs or IPs.
  - While polls fail, value gauges are not exported (as TapHome).
- Logs: one line per failed poll or refresh, never a token, never a response body containing tokens.

## Login helper

`python -m daikin_onecta_exporter.login` on the workstation: reads client id/secret from env, prints the authorize
URL to stderr (redirect URI from `DAIKIN_REDIRECT_URI`, default `https://localhost/`, which must match the portal
app), reads the pasted redirect URL (with `?code=`) from the terminal, exchanges it, and prints **only** the seed
JSON (`refresh_token`, `authorized_at`) to stdout, so the owner pipes it into OpenBao without it touching the screen.

## Chart `helm/daikin-onecta-exporter`

As TapHome's: `required` client secret name; Secret keys `client-id`, `client-secret`, `token-json`; env from
`secretKeyRef`; `enableServiceLinks: false`; port 9879; a PVC (`persistence.size` 1Gi, `storageClass` value,
default empty) mounted at `/data`; Deployment `strategy: Recreate` (RWO volume); `fsGroup: 65534`; `interval` value
→ `DAIKIN_INTERVAL`; ServiceMonitor interval 60s. Version == appVersion == image tag, starting 0.1.0.

## Alerts (group `daikin`, warning)

- `DaikinUnreachable`: `daikin_up == 0` for 1h.
- `DaikinLoginFailing`: `daikin_login_ok == 0` for 15m (summary: run the login helper again).
- `DaikinLoginExpiring`: `time() - daikin_authorized_timestamp_seconds > 335 * 86400` for 1h.
- `DaikinError`: `daikin_error_state == 1` for 10m.
- `DaikinTankCold`: `daikin_temperature_celsius{sensor="tankTemperature"} < 40` for 2h (default, chart value).

## Dashboard `dashboards/daikin.json`, uid `daikin`, title "Heat pump" (no folder prefix), Home folder

Now: outdoor, room, leaving water, tank, on/off and mode per management point, error state and code. Over time:
temperatures, setpoint vs actual (room / leaving water, tank), modes (state timeline), electricity per day
(heating and hot water, from `max_over_time(daikin_energy_yesterday_kwh[...])` shifted, or today's running value).
Exporter: up, login ok, last poll age, calls left today, login age in days.

## homenet_2.0 side

`kubernetes/apps/daikin-onecta-exporter/` as `taphome-exporter` (wave 60, namespace `daikin`, pinned tag, `manifests/`
with an ExternalSecret from `kv/daikin-onecta`: `client-id`, `client-secret`, `token-json`), storage class
`ceph-rbd`; runbook section with the owner steps.

## Left to the builder

Panel geometry; module internals as long as a scrape never calls the cloud; README wording.
