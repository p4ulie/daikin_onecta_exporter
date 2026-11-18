"""Read-only Daikin Onecta client: the token endpoint and GET /v1/gateway-devices, nothing else."""

import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://idp.onecta.daikineurope.com/v1/oidc/token"
AUTHORIZE_URL = "https://idp.onecta.daikineurope.com/v1/oidc/authorize"
API_URL = "https://api.onecta.daikineurope.com/v1/gateway-devices"
log = logging.getLogger("daikin_onecta_exporter.onecta")
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


def _oauth_error(e) -> str:
    """The OAuth `error` code (invalid_grant, invalid_client) as ", code"; the rest of the body may carry tokens."""
    try:
        err = json.load(e).get("error")
    except Exception:
        return ""
    return f", {err}" if isinstance(err, str) and re.fullmatch(r"[a-z_]{1,40}", err) else ""


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
        if e.code in (400, 401, 403):
            raise LoginError(f"token endpoint refused the grant (HTTP {e.code}{_oauth_error(e)})") from None
        raise OnectaError(f"token endpoint HTTP {e.code}") from None
    except Exception as e:
        raise OnectaError(f"token endpoint failed: {type(e).__name__}") from None


class Client:
    def __init__(self, client_id, client_secret, token_file, seed_json=None, opener=urllib.request.urlopen):
        self.client_id, self.client_secret, self.token_file, self.opener = client_id, client_secret, str(token_file), opener
        self.rate_limit: dict | None = None
        self.blocked_until = 0.0
        self.token_saved = True  # False while the rotated login lives only in memory
        self.tok, fresh_seed = self._load(seed_json)
        self.authorized_at = self.tok.get("authorized_at")
        if fresh_seed:
            self._persist()

    def _load(self, seed_json) -> tuple[dict, bool]:
        """The token dict and whether it is the seed. The file holds the rotated refresh token, so it wins
        unless the seed's authorized_at is newer: that is a fresh login from the helper."""
        stored = None
        try:
            with open(self.token_file) as f:
                stored = json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            stored = {}  # unreadable file: no refresh token, so login_ok 0 rather than a crash loop
        try:
            seed = json.loads(seed_json) if seed_json else {}
            seed = {"refresh_token": seed["refresh_token"], "authorized_at": seed.get("authorized_at")}
            newer = stored is None or (seed["authorized_at"] or 0) > (stored.get("authorized_at") or 0)
        except (ValueError, KeyError, TypeError, AttributeError):
            return (stored or {}), False
        return (seed, True) if newer else (stored, False)

    def _save(self):
        d = os.path.dirname(self.token_file) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".token-")  # mkstemp is 0600; same dir so the rename is atomic
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.tok, f)
                f.flush()
                os.fsync(f.fileno())  # the data must be on disk before the rename makes it the token file
            os.replace(tmp, self.token_file)
        except BaseException:
            os.unlink(tmp)
            raise
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)  # makes the rename itself durable
            finally:
                os.close(dfd)
        except OSError:
            pass  # best effort: the file is already in place

    def _persist(self):
        """Save without ever failing the poll; token_saved says whether the rotated login is on disk."""
        try:
            self._save()
            self.token_saved = True
        except OSError as e:
            self.token_saved = False
            log.warning("token save failed (%s), the login is only in memory; retrying on the next poll", type(e).__name__)

    def _access_token(self) -> str:
        if not self.token_saved:
            self._persist()
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
        self._persist()  # ponytail: a failed write is retried every poll; a restart before it works needs a new login
        return self.tok["access_token"]

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


def credentials() -> tuple[str, str]:
    # A stray newline (from `<<<` or a password manager paste) makes Daikin answer invalid_client.
    cid = os.environ.get("DAIKIN_CLIENT_ID", "").strip()
    secret = os.environ.get("DAIKIN_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        sys.exit("DAIKIN_CLIENT_ID and DAIKIN_CLIENT_SECRET are required")
    return cid, secret
