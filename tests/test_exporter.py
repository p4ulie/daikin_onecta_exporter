import copy
import json
import logging
import time
import urllib.error

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from daikin_onecta_exporter import exporter, onecta
from daikin_onecta_exporter.exporter import DaikinCollector, State, build, poll_interval, poll_once

from conftest import DEVICES, SECRET, FakeOpener, http_error

DEV = "00000000-0000-4000-8000-000000000001"


def render(state):
    registry = CollectorRegistry()
    registry.register(DaikinCollector(state))
    return generate_latest(registry).decode()


def make_client(tmp_path, opener):
    (tmp_path / "token.json").write_text(json.dumps(
        {"refresh_token": "ref-1", "access_token": "acc-0", "expires_at": time.time() + 3600, "authorized_at": 1700000000}))
    return onecta.Client("cid", SECRET, tmp_path / "token.json", opener=opener)


def polled(tmp_path, devices=None, **opener_kw):
    state = State(interval=600.0)
    poll_once(state, make_client(tmp_path, FakeOpener(devices=devices, **opener_kw)))
    return state


def line(out, prefix, *contains):
    return next(l for l in out.splitlines() if l.startswith(prefix) and all(c in l for c in contains))


def test_before_first_poll():
    out = render(State())
    assert "daikin_up 0.0" in out and "daikin_read_errors_total 0.0" in out and "daikin_login_ok 1.0" in out
    assert "daikin_temperature_celsius" not in out


def test_good_poll_all_metrics(tmp_path):
    out = render(polled(tmp_path))
    mp = f'device="{DEV}",management_point="climateControlMainZone"'
    tank = f'device="{DEV}",management_point="domesticHotWaterTank"'
    assert "daikin_up 1.0" in out and "daikin_login_ok 1.0" in out
    for m in ("daikin_last_success_timestamp_seconds ", "daikin_read_duration_seconds ", "daikin_poll_interval_seconds "):
        assert m in out
    assert "daikin_authorized_timestamp_seconds 1.7e+09" in out
    assert "daikin_ratelimit_remaining_day 150.0" in out and "daikin_ratelimit_remaining_minute 19.0" in out
    assert line(out, "daikin_temperature_celsius{", mp, 'sensor="outdoorTemperature"').endswith(" 4.5")
    assert line(out, "daikin_temperature_celsius{", tank, 'sensor="tankTemperature"').endswith(" 48.0")
    assert "returnWaterTemperature" not in out  # null is skipped
    assert line(out, "daikin_setpoint_celsius{", mp, 'operation_mode="heating"', 'setpoint="leavingWaterOffset"').endswith(" -2.0")
    assert line(out, "daikin_setpoint_celsius{", tank, 'setpoint="domesticHotWaterTemperature"').endswith(" 50.0")
    assert line(out, "daikin_on{", mp).endswith(" 1.0") and line(out, "daikin_on{", tank).endswith(" 0.0")
    assert line(out, "daikin_operation_mode_info{", mp, 'mode="heating"').endswith(" 1.0")
    assert line(out, "daikin_powerful_mode_active{", tank).endswith(" 1.0")
    assert "daikin_powerful_mode_active{" in out and "management_point=\"gateway\"" not in line(out, "daikin_powerful_mode_active{")
    assert line(out, "daikin_error_state{", tank).endswith(" 1.0") and line(out, "daikin_error_state{", mp).endswith(" 0.0")
    assert line(out, "daikin_warning_state{", mp).endswith(" 1.0")
    assert line(out, "daikin_emergency_state{", mp).endswith(" 0.0")
    assert line(out, "daikin_error_code_info{", tank, 'code="A1-01"').endswith(" 1.0")
    assert line(out, "daikin_error_code_info{", mp, 'code="-00"')
    assert line(out, "daikin_info{", 'management_point="gateway"', 'model="BRP069A78"', 'firmware="1.2.3"')
    assert line(out, "daikin_info{", 'management_point="indoorUnitHydro"', 'model="EHVX08S23EJ9W"', 'firmware=""')
    assert "management_point=\"climateControlMainZone\"" not in "".join(l for l in out.splitlines() if l.startswith("daikin_info{"))


def test_energy_sums_skip_null_and_missing(tmp_path):
    out = render(polled(tmp_path))
    mp = f'management_point="climateControlMainZone"'
    tank = f'management_point="domesticHotWaterTank"'
    assert line(out, "daikin_energy_yesterday_kwh{", mp, 'mode="heating"').endswith(" 3.0")  # 1+0+2
    assert line(out, "daikin_energy_today_kwh{", mp, 'mode="heating"').endswith(" 12.0")  # 3+4+5, nulls skipped
    assert line(out, "daikin_energy_today_kwh{", mp, 'mode="cooling"').endswith(" 0.0")
    assert line(out, "daikin_energy_today_kwh{", tank, 'mode="heating"').endswith(" 2.0")
    assert not [l for l in out.splitlines() if l.startswith("daikin_energy") and "gateway" in l]  # no consumptionData


def test_failed_poll_hides_value_gauges(tmp_path, caplog):
    state = polled(tmp_path)
    state_client = make_client(tmp_path, FakeOpener(api_error=urllib.error.URLError("down")))
    with caplog.at_level(logging.WARNING):
        poll_once(state, state_client)
    out = render(state)
    assert "daikin_up 0.0" in out and "daikin_read_errors_total 1.0" in out
    assert "daikin_temperature_celsius" not in out and "daikin_info" not in out
    assert "poll failed" in caplog.text


def test_invalid_grant_login_ok_zero(tmp_path, caplog):
    (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "ref-1", "expires_at": 0, "authorized_at": 1700000000}))
    c = onecta.Client("cid", SECRET, tmp_path / "token.json", opener=FakeOpener(token=http_error(400)))
    state = State()
    with caplog.at_level(logging.WARNING):
        poll_once(state, c)
    out = render(state)
    assert "daikin_login_ok 0.0" in out and "daikin_up 0.0" in out
    assert "daikin_authorized_timestamp_seconds 1.7e+09" in out
    assert "login" in caplog.text
    poll_once(state, make_client(tmp_path, FakeOpener()))  # a working login brings it back
    assert "daikin_login_ok 1.0" in render(state)


def test_429_skips_polls_until_retry_after(tmp_path, monkeypatch):
    op = FakeOpener(api_error=http_error(429, {"Retry-After": "300"}))
    c = make_client(tmp_path, op)
    state = State()
    poll_once(state, c)
    assert len(op.calls) == 1 and state.errors == 1
    assert "daikin_up 0.0" in render(state)
    poll_once(state, c)  # inside the window: no call, no new error
    assert len(op.calls) == 1 and state.errors == 1
    op.api_error = None
    now = time.time()
    monkeypatch.setattr("daikin_onecta_exporter.exporter.time.time", lambda: now + 301)
    monkeypatch.setattr("daikin_onecta_exporter.onecta.time.time", lambda: now + 301)
    c.tok["expires_at"] = now + 10000
    poll_once(state, c)
    assert len(op.calls) == 2 and "daikin_up 1.0" in render(state)


def test_ratelimit_gauges_follow_the_429_headers(tmp_path):
    out = render(polled(tmp_path, api_error=http_error(429, {"Retry-After": "60", "X-RateLimit-Remaining-day": "0"})))
    assert "daikin_ratelimit_remaining_day 0.0" in out


def bad(mutate):
    d = copy.deepcopy(DEVICES)
    return mutate(d)


@pytest.mark.parametrize("devices", [
    [],
    {},
    bad(lambda d: [{"id": "x"}]),  # no managementPoints
    bad(lambda d: [{"managementPoints": []}]),  # no id
    bad(lambda d: [{"id": "x", "managementPoints": [{}]}]),  # no embeddedId
    bad(lambda d: [{"id": "x", "managementPoints": [{"embeddedId": "a", "sensoryData": {"value": 7}}]}]),
])
def test_malformed_reply_is_a_failed_poll(tmp_path, caplog, devices):
    state = State()
    with caplog.at_level(logging.WARNING):
        poll_once(state, make_client(tmp_path, FakeOpener(devices=devices)))
    out = render(state)
    assert "daikin_up 0.0" in out and "daikin_read_errors_total 1.0" in out and "daikin_login_ok 1.0" in out
    assert "poll failed" in caplog.text


def test_build_without_optional_parts():
    s = build([{"id": "x", "managementPoints": [{"embeddedId": "gateway"}]}])
    assert s == {}


def test_poll_interval_clamp_and_warning(monkeypatch, caplog):
    monkeypatch.delenv("DAIKIN_INTERVAL", raising=False)
    with caplog.at_level(logging.WARNING):
        assert poll_interval() == 600
    assert caplog.text == ""
    monkeypatch.setenv("DAIKIN_INTERVAL", "10")
    with caplog.at_level(logging.WARNING):
        assert poll_interval() == 60
    assert "60" in caplog.text and "calls/day" in caplog.text
    caplog.clear()
    monkeypatch.setenv("DAIKIN_INTERVAL", "500")
    with caplog.at_level(logging.WARNING):
        assert poll_interval() == 500
    assert caplog.text == ""
    caplog.clear()
    monkeypatch.setenv("DAIKIN_INTERVAL", "300")  # 288 calls a day
    with caplog.at_level(logging.WARNING):
        poll_interval()
    assert "288" in caplog.text


def test_no_secret_in_logs(tmp_path, caplog):
    leak = urllib.error.URLError(f"x ref-1 acc-0 {SECRET} acc-9")
    with caplog.at_level(logging.DEBUG):
        for kw in ({"api_error": leak}, {"token": leak}):
            (tmp_path / "token.json").write_text(json.dumps(
                {"refresh_token": "ref-1", "access_token": "acc-0", "expires_at": 0 if "token" in kw else time.time() + 3600}))
            poll_once(State(), onecta.Client("cid", SECRET, tmp_path / "token.json", opener=FakeOpener(**kw)))
    assert "poll failed" in caplog.text
    for s in ("ref-1", "acc-0", "acc-9", SECRET):
        assert s not in caplog.text


def test_missing_env_exits(monkeypatch):
    monkeypatch.delenv("DAIKIN_CLIENT_ID", raising=False)
    monkeypatch.delenv("DAIKIN_CLIENT_SECRET", raising=False)
    with pytest.raises(SystemExit):
        exporter.main()


def test_build_skips_non_dict_entries():
    mp = {"embeddedId": "a", "sensoryData": {"value": {"t": None, "u": "x", "ok": {"value": 1}}},
          "temperatureControl": {"value": {"operationModes": {"heating": {"setpoints": {"s": None, "o": {"value": 2}}}, "x": None}}},
          "consumptionData": {"value": {"electrical": {"unit": "kWh", "heating": None}}}}
    s = build([{"id": "x", "managementPoints": [mp]}])
    assert s == {"daikin_temperature_celsius": [(["x", "a", "ok"], 1.0)], "daikin_setpoint_celsius": [(["x", "a", "heating", "o"], 2.0)]}


def failing_save(monkeypatch, fails):
    """Make the first `fails` saves raise OSError."""
    real, n = onecta.Client._save, [0]

    def save(self):
        n[0] += 1
        if n[0] <= fails:
            raise OSError("disk full")
        real(self)
    monkeypatch.setattr(onecta.Client, "_save", save)
    return n


def test_failed_token_save_keeps_poll_and_retries(tmp_path, monkeypatch, caplog):
    failing_save(monkeypatch, 2)
    (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "ref-1", "expires_at": 0, "authorized_at": 1700000000}))
    c = onecta.Client("cid", SECRET, tmp_path / "token.json", opener=FakeOpener())
    state = State()
    with caplog.at_level(logging.WARNING):
        poll_once(state, c)
        out = render(state)
        assert "daikin_up 1.0" in out and "daikin_token_saved 0.0" in out  # poll survives, rotated login only in memory
        poll_once(state, c)  # retry fails again
        assert "daikin_token_saved 0.0" in render(state)
        poll_once(state, c)  # third save works
    assert "daikin_token_saved 1.0" in render(state)
    assert json.loads((tmp_path / "token.json").read_text())["refresh_token"] == "ref-2"
    assert caplog.text.count("token save failed") == 2
    for s in ("ref-1", "ref-2", "acc-1", SECRET):
        assert s not in caplog.text


def test_token_saved_is_one_before_first_poll():
    assert "daikin_token_saved 1.0" in render(State())
