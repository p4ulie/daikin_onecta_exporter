"""Read-only Daikin Onecta client: the token endpoint and GET /v1/gateway-devices, nothing else."""

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://idp.onecta.daikineurope.com/v1/oidc/token"
AUTHORIZE_URL = "https://idp.onecta.daikineurope.com/v1/oidc/authorize"
API_URL = "https://api.onecta.daikineurope.com/v1/gateway-devices"
SCOPE = "openid onecta:basic.integration offline_access"
REFRESH_MARGIN = 300  # seconds of access token left that trigger a refresh
DEFAULT_RETRY_AFTER = 300  # a 429 without the header


class OnectaError(Exception):
    """Messages are fixed strings plus a status code or exception class: never a token, secret or body."""


class LoginError(OnectaError):
    """The refresh token or authorization code was rejected: only a new login helps."""


class RateLimited(OnectaError):
    def __init__(self, retry_after):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


def _int(headers, name):
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


def rate_limit_from(headers) -> dict:
    return {"limit_day": _int(headers, "X-RateLimit-Limit-day"), "remaining_day": _int(headers, "X-RateLimit-Remaining-day"),
            "limit_minute": _int(headers, "X-RateLimit-Limit-minute"),
            "remaining_minute": _int(headers, "X-RateLimit-Remaining-minute"), "retry_after": _int(headers, "Retry-After")}


def token_request(opener, form: dict) -> dict:
    """POST the form to the token endpoint; client id and secret ride in the body, which the OAuth spec allows."""
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(req, timeout=15) as r:
            body = json.load(r)
        if not isinstance(body, dict) or not body.get("access_token"):
            raise ValueError
        return body
    except urllib.error.HTTPError as e:
        # from None everywhere: the URL or body of the original error may carry a token
        if e.code in (400, 401):
            raise LoginError(f"token endpoint refused the grant (HTTP {e.code})") from None
        raise OnectaError(f"token endpoint HTTP {e.code}") from None
    except Exception as e:
        raise OnectaError(f"token endpoint failed: {type(e).__name__}") from None


class Client:
    def __init__(self, client_id, client_secret, token_file, seed_json=None, opener=urllib.request.urlopen):
        self.client_id, self.client_secret, self.token_file, self.opener = client_id, client_secret, str(token_file), opener
        self.rate_limit: dict | None = None
        self.blocked_until = 0.0
        self.tok = self._load(seed_json)
        self.authorized_at = self.tok.get("authorized_at")

    def _load(self, seed_json) -> dict:
        # The file wins: it holds the rotated refresh token; the seed is only for an empty volume.
        try:
            with open(self.token_file) as f:
                return json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            return {}  # unreadable file: no refresh token, so login_ok 0 rather than a crash loop
        try:
            seed = json.loads(seed_json) if seed_json else {}
            return {"refresh_token": seed["refresh_token"], "authorized_at": seed.get("authorized_at")}
        except (ValueError, KeyError, TypeError):
            return {}

    def _save(self):
        d = os.path.dirname(self.token_file) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".token-")  # mkstemp is 0600; same dir so the rename is atomic
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.tok, f)
            os.replace(tmp, self.token_file)
        except BaseException:
            os.unlink(tmp)
            raise

    def _access_token(self) -> str:
        t = self.tok
        if t.get("access_token") and t.get("expires_at", 0) - time.time() > REFRESH_MARGIN:
            return t["access_token"]
        if not t.get("refresh_token"):
            raise LoginError("no refresh token: run the login helper")
        body = token_request(self.opener, {"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                           "client_id": self.client_id, "client_secret": self.client_secret})
        # The refresh token rotates; keep the old one if a reply omits it.
        self.tok = {**t, "access_token": body["access_token"], "refresh_token": body.get("refresh_token") or t["refresh_token"],
                    "expires_at": time.time() + float(body.get("expires_in", 3600))}
        self._save()  # ponytail: a failed write raises once; the in-memory token keeps this process going
        return self.tok["access_token"]

    @property
    def expires_at(self):
        return self.tok.get("expires_at", 0)

    @expires_at.setter
    def expires_at(self, v):
        self.tok["expires_at"] = v

    def get_devices(self) -> list:
        if time.time() < self.blocked_until:
            raise RateLimited(round(self.blocked_until - time.time()))
        req = urllib.request.Request(API_URL, headers={"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"})
        try:
            with self.opener(req, timeout=20) as r:
                self.rate_limit = rate_limit_from(r.headers)
                return json.load(r)
        except urllib.error.HTTPError as e:
            self.rate_limit = rate_limit_from(e.headers)
            if e.code == 429:
                retry = self.rate_limit["retry_after"] or DEFAULT_RETRY_AFTER
                self.blocked_until = time.time() + retry
                raise RateLimited(retry) from None
            if e.code in (401, 403):
                self.tok["expires_at"] = 0  # a revoked access token: refresh on the next poll
            raise OnectaError(f"API HTTP {e.code}") from None
        except Exception as e:
            raise OnectaError(f"API request failed: {type(e).__name__}") from None
