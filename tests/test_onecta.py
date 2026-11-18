import io
import json
import logging
import os
import time
import urllib.error

import pytest

from daikin_onecta_exporter import onecta
from daikin_onecta_exporter.onecta import Client, LoginError, RateLimited

from conftest import SECRET, FakeOpener, http_error


def token_file(tmp_path, **kw):
    p = tmp_path / "token.json"
    data = {"refresh_token": "ref-1", "access_token": "acc-0", "expires_at": time.time() + 3600, "authorized_at": 1700000000}
    p.write_text(json.dumps({**data, **kw}))
    return p


def client(path, opener, seed=None):
    return Client("cid", SECRET, path, seed_json=seed, opener=opener)


def test_fresh_token_is_not_refreshed(tmp_path):
    op = FakeOpener()
    c = client(token_file(tmp_path), op)
    assert len(c.get_devices()) == 1
    assert all("/oidc/token" not in u for u in op.urls())
    assert op.calls[0][2] == "Bearer acc-0"
    assert c.rate_limit == {"limit_day": 200, "remaining_day": 150, "limit_minute": 20, "remaining_minute": 19, "retry_after": None}


def test_near_expiry_refreshes_and_rewrites_file_atomically(tmp_path):
    path = token_file(tmp_path, expires_at=time.time() + 100)
    op = FakeOpener()
    c = client(path, op)
    c.get_devices()
    url, form, _ = op.calls[0]
    assert url.endswith("/oidc/token")
    assert form == {"grant_type": "refresh_token", "refresh_token": "ref-1", "client_id": "cid", "client_secret": SECRET}
    assert op.calls[1][2] == "Bearer acc-1"
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "ref-2" and saved["access_token"] == "acc-1"
    assert saved["authorized_at"] == 1700000000 and c.authorized_at == 1700000000
    assert saved["expires_at"] > time.time() + 3000
    assert os.listdir(tmp_path) == ["token.json"]  # temp file renamed away
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_restart_keeps_rotated_token(tmp_path):
    path = token_file(tmp_path, expires_at=0)
    client(path, FakeOpener()).get_devices()
    op = FakeOpener()
    client(path, op, seed=json.dumps({"refresh_token": "stale", "authorized_at": 1})).get_devices()
    assert all("/oidc/token" not in u for u in op.urls())  # rotated token reused, seed ignored


def test_seed_used_only_when_file_missing(tmp_path):
    path = tmp_path / "token.json"
    op = FakeOpener()
    c = client(path, op, seed=json.dumps({"refresh_token": "seed-ref", "authorized_at": 1700000001}))
    c.get_devices()
    assert op.calls[0][1]["refresh_token"] == "seed-ref"
    saved = json.loads(path.read_text())
    assert saved["authorized_at"] == 1700000001 and saved["refresh_token"] == "ref-2"
    # file exists now: a different seed is ignored
    op2 = FakeOpener()
    client(path, op2, seed=json.dumps({"refresh_token": "other", "authorized_at": 5})).get_devices()
    assert all("/oidc/token" not in u for u in op2.urls())


def test_no_token_and_no_seed_is_a_login_error(tmp_path):
    with pytest.raises(LoginError):
        client(tmp_path / "token.json", FakeOpener()).get_devices()


@pytest.mark.parametrize("code", [400, 401, 403])
def test_invalid_grant_is_login_error(tmp_path, code):
    path = token_file(tmp_path, expires_at=0)
    c = client(path, FakeOpener(token=http_error(code)))
    with pytest.raises(LoginError):
        c.get_devices()
    assert json.loads(path.read_text())["refresh_token"] == "ref-1"  # untouched


def test_429_sets_block_until_retry_after(tmp_path):
    c = client(token_file(tmp_path), FakeOpener(api_error=http_error(429, {"Retry-After": "120", "X-RateLimit-Remaining-day": "0"})))
    with pytest.raises(RateLimited) as e:
        c.get_devices()
    assert e.value.retry_after == 120
    assert 100 < c.blocked_until - time.time() <= 120
    assert c.rate_limit["remaining_day"] == 0 and c.rate_limit["retry_after"] == 120


def test_api_401_forces_refresh_next_time(tmp_path):
    path = token_file(tmp_path)
    c = client(path, FakeOpener(api_error=http_error(401)))
    with pytest.raises(onecta.OnectaError):
        c.get_devices()
    op = FakeOpener()
    c.opener = op
    c.get_devices()
    assert op.calls[0][0].endswith("/oidc/token")


def test_errors_never_carry_secrets(tmp_path, caplog):
    leak = urllib.error.URLError(f"boom refresh_token=ref-1 secret={SECRET} acc-0")
    for op in (FakeOpener(api_error=leak), FakeOpener(token=leak)):
        path = token_file(tmp_path, expires_at=0 if op.token else time.time() + 3600)
        with pytest.raises(onecta.OnectaError) as e:
            client(path, op).get_devices()
        for s in ("ref-1", SECRET, "acc-0"):
            assert s not in str(e.value) and s not in repr(e.value)
            assert e.value.__cause__ is None and e.value.__suppress_context__


def test_save_fsyncs_file_and_directory_before_rename(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(onecta.os, "fsync", lambda fd: events.append("fsync"))
    real = os.replace
    monkeypatch.setattr(onecta.os, "replace", lambda a, b: (events.append("replace"), real(a, b)))
    client(token_file(tmp_path, expires_at=0), FakeOpener()).get_devices()
    assert events == ["fsync", "replace", "fsync"]  # file, rename, then the directory


def test_login_error_logs_oauth_error_field_only(tmp_path):
    for code in (400, 401, 403):
        c = client(token_file(tmp_path, expires_at=0), FakeOpener(token=http_error(code)))
        with pytest.raises(LoginError) as e:
            c.get_devices()
        assert "invalid_grant" in str(e.value) and str(code) in str(e.value)
    odd = urllib.error.HTTPError("https://x", 400, "e", {}, io.BytesIO(b'{"error":"refresh_token=ref-1 acc-0"}'))
    with pytest.raises(LoginError) as e:
        client(token_file(tmp_path, expires_at=0), FakeOpener(token=odd)).get_devices()
    assert "ref-1" not in str(e.value)  # only a plain error code is passed on


def test_newer_seed_wins_and_is_written(tmp_path):
    path = token_file(tmp_path)  # authorized_at 1700000000
    op = FakeOpener()
    c = client(path, op, seed=json.dumps({"refresh_token": "seed-ref", "authorized_at": 1800000000}))
    assert c.authorized_at == 1800000000
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "seed-ref" and saved["authorized_at"] == 1800000000
    c.get_devices()
    assert op.calls[0][1]["refresh_token"] == "seed-ref"  # no stale access token from the old file


def test_older_or_equal_seed_loses(tmp_path):
    path = token_file(tmp_path)
    for at in (1600000000, 1700000000):
        client(path, FakeOpener(), seed=json.dumps({"refresh_token": "seed-ref", "authorized_at": at}))
        assert json.loads(path.read_text())["refresh_token"] == "ref-1"
