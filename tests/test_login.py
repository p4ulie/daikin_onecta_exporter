import io
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from daikin_onecta_exporter import login

from conftest import SECRET, FakeOpener, http_error


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DAIKIN_CLIENT_ID", "cid")
    monkeypatch.setenv("DAIKIN_CLIENT_SECRET", SECRET)
    monkeypatch.delenv("DAIKIN_REDIRECT_URI", raising=False)


def test_prints_only_seed_json_on_stdout(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("https://localhost/?code=abc123&state=x\n"))
    op = FakeOpener(token={"access_token": "tok-aaa", "refresh_token": "ref-new", "expires_in": 3600})
    login.main(opener=op)
    out, err = capsys.readouterr()
    seed = json.loads(out)
    assert set(seed) == {"refresh_token", "authorized_at"}
    assert seed["refresh_token"] == "ref-new" and abs(seed["authorized_at"] - time.time()) < 5
    assert out.count("\n") == 1
    assert "tok-aaa" not in err and "ref-new" not in err and SECRET not in err
    url = next(w for w in err.split() if w.startswith("https://idp.onecta.daikineurope.com/v1/oidc/authorize"))
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["https://localhost/"] and q["response_type"] == ["code"]
    assert q["scope"] == ["openid onecta:basic.integration offline_access"]
    _, form, _ = op.calls[0]
    assert form == {"grant_type": "authorization_code", "code": "abc123", "redirect_uri": "https://localhost/",
                    "client_id": "cid", "client_secret": SECRET}


def test_failed_exchange_prints_nothing_on_stdout(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("https://localhost/?code=old\n"))
    with pytest.raises(SystemExit) as e:
        login.main(opener=FakeOpener(token=http_error(400)))
    assert e.value.code  # sys.exit(message): exit status 1, text on stderr
    out, err = capsys.readouterr()
    assert out == "" and "again" in e.value.code and SECRET not in err + e.value.code


def test_pasted_address_without_code_exits(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("https://localhost/?error=access_denied\n"))
    with pytest.raises(SystemExit):
        login.main(opener=FakeOpener())
    assert capsys.readouterr().out == ""


def test_missing_env_exits(monkeypatch):
    monkeypatch.delenv("DAIKIN_CLIENT_ID", raising=False)
    monkeypatch.delenv("DAIKIN_CLIENT_SECRET", raising=False)
    with pytest.raises(SystemExit):
        login.main(opener=FakeOpener())
