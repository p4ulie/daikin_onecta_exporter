"""Prometheus exporter: a background thread polls the Onecta cloud, scrapes only format the last result."""

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily

from . import onecta

MIN_INTERVAL = 60.0
MAX_CALLS_PER_DAY = 180  # Daikin allows about 200; keep headroom
log = logging.getLogger("daikin_onecta_exporter.exporter")

BASE = ("device", "management_point")
# name -> (help, extra label names)
FAMILIES = {
    "daikin_temperature_celsius": ("Temperature reported by the unit", ("sensor",)),
    "daikin_setpoint_celsius": ("Setpoint per operation mode", ("operation_mode", "setpoint")),
    "daikin_on": ("1 if onOffMode is on", ()),
    "daikin_operation_mode_info": ("Current operation mode", ("mode",)),
    "daikin_powerful_mode_active": ("1 if powerful mode is active", ()),
    "daikin_error_state": ("1 if the unit is in an error state", ()),
    "daikin_warning_state": ("1 if the unit is in a warning state", ()),
    "daikin_emergency_state": ("1 if the unit is in an emergency state", ()),
    "daikin_error_code_info": ("Error code as the API sends it", ("code",)),
    "daikin_energy_today_kwh": ("Electricity used today so far, whole kWh buckets", ("mode",)),
    "daikin_energy_yesterday_kwh": ("Electricity used yesterday, whole kWh buckets", ("mode",)),
    "daikin_info": ("Model and firmware where the management point reports them", ("model", "firmware")),
}


@dataclass
class State:
    samples: dict | None = None  # metric name -> [(label values, value)]; kept out of /metrics while polls fail
    ok: bool = False
    login_ok: bool = True  # unknown before the first poll counts as fine, so no alert on a fresh start
    last_success: float | None = None
    duration: float | None = None
    errors: int = 0
    rate_limit: dict = field(default_factory=dict)
    authorized_at: float | None = None
    interval: float | None = None


def _v(obj, key):
    """The {"value": ...} of a characteristic, None when absent."""
    c = obj.get(key)
    return c.get("value") if isinstance(c, dict) else None


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def build(devices) -> dict:
    """Turn the reply into samples; any wrong shape raises, so the poll counts as failed."""
    # An empty list is a wrong shape too: the account has no devices when the portal login differs from Onecta's.
    if not isinstance(devices, list) or not devices:
        raise ValueError("no devices in reply")
    samples: dict[str, list] = {}

    def add(name, base, extra, value):
        samples.setdefault(name, []).append(([*base, *map(str, extra)], float(value)))

    for dev in devices:
        dev_id = str(dev["id"])  # the gateway device id is the `device` label
        for mp in dev["managementPoints"]:
            base = (dev_id, str(mp["embeddedId"]))
            for sensor, c in (_v(mp, "sensoryData") or {}).items():
                if _num(c.get("value")):
                    add("daikin_temperature_celsius", base, (sensor,), c["value"])
            for mode, m in ((_v(mp, "temperatureControl") or {}).get("operationModes") or {}).items():
                for name, c in (m.get("setpoints") or {}).items():
                    if _num(c.get("value")):
                        add("daikin_setpoint_celsius", base, (mode, name), c["value"])
            if (on := _v(mp, "onOffMode")) is not None:
                add("daikin_on", base, (), on == "on")
            if isinstance(mode := _v(mp, "operationMode"), str):
                add("daikin_operation_mode_info", base, (mode,), 1)
            for name, key in (("daikin_powerful_mode_active", "isPowerfulModeActive"), ("daikin_error_state", "isInErrorState"),
                              ("daikin_warning_state", "isInWarningState"), ("daikin_emergency_state", "isInEmergencyState")):
                if isinstance(b := _v(mp, key), bool):
                    add(name, base, (), b)
            if isinstance(code := _v(mp, "errorCode"), str):
                add("daikin_error_code_info", base, (code,), 1)
            for mode, c in ((_v(mp, "consumptionData") or {}).get("electrical") or {}).items():
                d = c.get("d")
                if isinstance(d, list) and len(d) == 24:
                    # 12 two-hour buckets of yesterday, then 12 of today; null is the future
                    add("daikin_energy_yesterday_kwh", base, (mode,), sum(x for x in d[:12] if _num(x)))
                    add("daikin_energy_today_kwh", base, (mode,), sum(x for x in d[12:] if _num(x)))
            model, fw = _v(mp, "modelInfo"), _v(mp, "firmwareVersion")
            if model or fw:
                add("daikin_info", base, (model or "", fw or ""), 1)
    return samples


def poll_once(state: State, client: onecta.Client) -> None:
    if time.time() < client.blocked_until:
        return  # inside a 429 window: no call, nothing new to record
    start = time.monotonic()
    try:
        samples = build(client.get_devices())  # build inside the try: a malformed reply is a failed poll
    except Exception as e:  # login, 429, timeout, bad JSON or shape: record it, keep polling
        state.duration = time.monotonic() - start
        state.errors += 1
        state.ok = False
        if isinstance(e, onecta.LoginError):
            state.login_ok = False
            log.warning("login failed, run the login helper again: %s", e)
        else:
            log.warning("poll failed: %s: %s", type(e).__name__, e)  # our errors carry no secrets; others are shape errors
    else:
        state.duration = time.monotonic() - start
        state.samples, state.ok, state.login_ok = samples, True, True
        state.last_success = time.time()
    state.rate_limit = client.rate_limit or state.rate_limit
    state.authorized_at = client.authorized_at


class DaikinCollector:
    def __init__(self, state: State):
        self.state = state

    def collect(self):
        s = self.state  # ponytail: no lock; a scrape racing a poll can mix two fields, harmless for gauges
        ok, samples = s.ok, s.samples  # one snapshot so up and the value gauges never disagree
        yield GaugeMetricFamily("daikin_up", "1 if the last poll of the cloud succeeded", value=float(ok))
        yield GaugeMetricFamily("daikin_login_ok", "0 when Daikin rejected the refresh token", value=float(s.login_ok))
        if s.last_success is not None:
            yield GaugeMetricFamily("daikin_last_success_timestamp_seconds", "Unix time of the last successful poll", value=s.last_success)
        if s.duration is not None:
            yield GaugeMetricFamily("daikin_read_duration_seconds", "Duration of the last poll", value=s.duration)
        yield CounterMetricFamily("daikin_read_errors", "Failed polls", value=s.errors)
        if s.authorized_at is not None:
            yield GaugeMetricFamily("daikin_authorized_timestamp_seconds", "Unix time of the login", value=s.authorized_at)
        for key in ("remaining_day", "remaining_minute"):
            if s.rate_limit.get(key) is not None:
                yield GaugeMetricFamily(f"daikin_ratelimit_{key}", f"Calls left ({key.split('_')[1]}) per Daikin's headers", value=s.rate_limit[key])
        if s.interval is not None:
            yield GaugeMetricFamily("daikin_poll_interval_seconds", "Seconds between polls", value=s.interval)
        if not ok or samples is None:
            return
        for name, rows in samples.items():
            help_, extra = FAMILIES[name]
            fam = GaugeMetricFamily(name, help_, labels=(*BASE, *extra))
            for labels, value in rows:
                fam.add_metric(labels, value)
            yield fam


def poll_interval() -> float:
    raw = float(os.environ.get("DAIKIN_INTERVAL", "600"))
    interval = max(MIN_INTERVAL, raw)
    if interval != raw:
        log.warning("DAIKIN_INTERVAL %g is below the minimum, using %g", raw, interval)
    if (calls := 86400 / interval) > MAX_CALLS_PER_DAY:
        log.warning("interval %gs is %.0f calls/day, over the %d the Daikin limit leaves room for", interval, calls, MAX_CALLS_PER_DAY)
    return interval


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cid, secret = os.environ.get("DAIKIN_CLIENT_ID"), os.environ.get("DAIKIN_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("DAIKIN_CLIENT_ID and DAIKIN_CLIENT_SECRET are required")
    interval = poll_interval()
    port = int(os.environ.get("DAIKIN_EXPORTER_PORT", "9879"))
    client = onecta.Client(cid, secret, os.environ.get("DAIKIN_TOKEN_FILE", "/data/token.json"),
                           seed_json=os.environ.get("DAIKIN_TOKEN_JSON"))
    state = State(interval=interval, authorized_at=client.authorized_at)
    REGISTRY.register(DaikinCollector(state))
    start_http_server(port)
    print(f"daikin exporter on :{port}/metrics, every {interval:g}s", flush=True)

    def loop():
        while True:
            poll_once(state, client)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
    threading.Event().wait()  # daemon thread dies with the main thread


if __name__ == "__main__":
    main()
