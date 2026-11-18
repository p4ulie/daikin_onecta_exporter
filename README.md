# daikin_onecta_exporter

Prometheus exporter for a Daikin Altherma heat pump, read through Daikin's **Onecta cloud API**. Built for an
Altherma 3 H (`EHVX08S23EJ9W`) with the `BRP069A78` LAN adapter, which has no local API: the cloud is the only
network route. Read-only: it never changes a setting on the unit.

It polls `GET /v1/gateway-devices` every 10 minutes (configurable, `DAIKIN_INTERVAL`), well under Daikin's limit of
about 200 calls a day, and serves the last result on `/metrics`. A Helm chart (`helm/daikin-onecta-exporter`) adds a
ServiceMonitor, alerts and a "Heat pump" Grafana dashboard.

Design: `docs/superpowers/specs/2026-11-18-daikin-onecta-exporter-design.md`.

## Running locally

```sh
cp .env.example .env     # fill in DAIKIN_CLIENT_ID and DAIKIN_CLIENT_SECRET
mise run test            # unit tests, no network
mise run exporter        # runs through uv with requirements.txt; needs a token: DAIKIN_TOKEN_FILE (or DAIKIN_TOKEN_JSON as a seed) from the login helper
curl -s localhost:9879/metrics | grep ^daikin_
```

Point `DAIKIN_TOKEN_FILE` at a writable path (the default `/data/token.json` is the container's volume). The
exporter writes only that file's directory, so it runs fine on a read-only root filesystem. `mise run image:build`
builds the image locally; `image:push` is described below.

## Connecting to your Daikin account

Daikin's API uses OAuth2. You register your own "app" on Daikin's developer portal, authorize it once in a browser
with your Onecta account, and give the exporter the resulting refresh token. After that the exporter renews its own
access, and you repeat the authorization about once a year (the `DaikinLoginExpiring` alert says when).

Do the steps in this order.

### 1. Developer portal account

1. Open https://developer.cloud.daikineurope.com.
2. Sign up **with the same email address and the same login method you use in the Onecta app** (for example, if you
   log in to Onecta with Google, sign up with Google). With a different account the API returns no devices.

### 2. Create the app

1. In the portal, open **My Apps** and create a new app. The name is only for you, e.g. `homelab-exporter`.
2. Set the **redirect URI** to an `https://` address on a domain you control, e.g.
   `https://grafana.home.example.net/daikin`. The portal refuses `https://localhost` and `http://`. Nothing has to
   answer there: the browser may show a 404 or an error page, and you copy that page's address, which carries the
   one-time code. A host of your own keeps the code off other people's servers.
3. Save. The portal shows the **client ID** and the **client secret**. Copy the secret now; the portal may show it
   only once. Keep both in your password manager.

### 3. Authorize once and get the refresh token

From a clone of this repository, on your workstation, run the login helper. The shell's `read` takes the client ID
and secret without echoing them. The helper prints a link and writes only the token (as JSON) to its standard
output, so it never appears on screen. Run it under `umask 077` so the token file is private:

```sh
read -r "DAIKIN_CLIENT_ID?Client ID: " && read -rs "DAIKIN_CLIENT_SECRET?Client secret: " && echo
export DAIKIN_CLIENT_ID DAIKIN_CLIENT_SECRET
export DAIKIN_REDIRECT_URI=https://grafana.home.example.net/daikin   # exactly as registered in step 2
(umask 077; mise run login > /tmp/daikin-token.json)    # or: python -m daikin_onecta_exporter.login
```

1. Open the link it prints in a browser and log in with your Onecta account.
2. Tick the consent checkbox and press **I agree**.
3. The browser lands on your redirect address with `?code=…` added (a 404 or error page is expected). Copy the **whole address** from the
   address bar and paste it into the helper.
4. The helper exchanges the code and writes the token JSON. It never prints the token to the terminal.

The code in that address works once and only for a few minutes. If the helper says it expired, run it again.

### 4. Store the credentials

For the homenet_2.0 deployment, all three values go into OpenBao at `kv/daikin-onecta` (the ExternalSecret reads them
from there), then the temporary file goes away:

```sh
printf %s "$DAIKIN_CLIENT_SECRET" |
  bao kv put kv/daikin-onecta client-id="$DAIKIN_CLIENT_ID" client-secret=- token-json=@/tmp/daikin-token.json
rm /tmp/daikin-token.json
unset DAIKIN_CLIENT_ID DAIKIN_CLIENT_SECRET
```

Elsewhere, create a Kubernetes Secret with the keys `client-id`, `client-secret` and `token-json`, and name it in
the chart's values.

### 5. Renewing the login

The exporter keeps the current token on its volume (`/data/token.json`) and rewrites it whenever Daikin issues a new
one. The copy in OpenBao is only a seed: the exporter uses it when the volume is empty or when its `authorized_at` is
newer than the file's. When `DaikinLoginFailing` or `DaikinLoginExpiring` fires, run the login helper (step 3),
update OpenBao (step 4), and restart the pod; the newer seed replaces the token file, nothing is deleted.

If `DaikinTokenNotSaved` fires, the exporter still works but the rotated login is only in memory, so a restart would
need a new browser login. Check the `/data` volume (full, read-only, wrong ownership); the exporter retries the write
on every poll and the alert clears by itself.

## Helm chart

```sh
helm install daikin helm/daikin-onecta-exporter --set onecta.secretName=daikin-onecta
```

| Value | Default | Meaning |
|---|---|---|
| `image.repository` | `10.34.1.1:30095/daikin-onecta-exporter` | Image to run |
| `image.tag` | `""` | Empty means the chart's `appVersion` |
| `onecta.secretName` | none | Required existing Secret with keys `client-id`, `client-secret`, `token-json`; the chart creates none |
| `onecta.interval` | `600` | Poll interval in seconds (Daikin allows about 200 calls a day). The dashboard's history panels step at 15 minutes, homenet's interval |
| `persistence.size`, `.storageClass` | `1Gi`, `""` | RWO volume at `/data` for the rotated refresh token; empty class means the cluster default |
| `resources` | 10m CPU, 32Mi request, 96Mi limit | Pod resources |
| `serviceMonitor.enabled`, `.interval` | `false`, `60s` | Prometheus Operator scrape config |
| `prometheusRule.enabled` | `false` | Alerts: unreachable, login failing, login expiring, token not saved, error, tank cold |
| `alerts.tankMin` | `40` | Tank temperature (C) below which `DaikinTankCold` fires after 2h |
| `dashboard.enabled`, `.labels`, `.annotations` | `false` | The "Heat pump" Grafana dashboard ConfigMap |

The chart installs no CRDs. The ServiceMonitor and PrometheusRule need prometheus-operator, the dashboard needs the Grafana sidecar.
The Deployment uses `Recreate` because the volume is ReadWriteOnce. The PVC carries `helm.sh/resource-policy: keep`: it holds the live login, so `helm uninstall` leaves it.

## Pushing changes and releasing

Changes are committed straight to `main` and pushed:

```sh
git push origin main
```

A release is a separate act, done when a version should reach the cluster:

1. Bump `version` and `appVersion` (the same value) in `helm/daikin-onecta-exporter/Chart.yaml`, commit, and tag:
   `git tag -a v0.1.1 -m v0.1.1 && git push origin main v0.1.1`.
2. Build and push the image to the lab registry: `mise run image:push` (tag = chart version).
3. In homenet_2.0, set `targetRevision` in `kubernetes/apps/daikin-onecta-exporter/application.yaml` to the new tag
   and commit to `main`; ArgoCD deploys it.
