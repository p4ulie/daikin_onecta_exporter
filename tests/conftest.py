import io
import json
import urllib.error
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"
DEVICES = json.loads((FIX / "gateway-devices.json").read_text())
SECRET = "s3cret-value"
LIMITS = {"X-RateLimit-Limit-day": "200", "X-RateLimit-Remaining-day": "150",
          "X-RateLimit-Limit-minute": "20", "X-RateLimit-Remaining-minute": "19"}


class Resp(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://x", code, "err", headers or {}, io.BytesIO(b'{"error":"invalid_grant"}'))


class FakeOpener:
    """Stands in for urlopen: answers the token endpoint and the devices endpoint, records every call."""

    def __init__(self, devices=None, token=None, api_error=None):
        self.devices = DEVICES if devices is None else devices
        self.token = token  # dict, or an exception to raise
        self.api_error = api_error
        self.calls = []  # (url, form dict or None, auth header)

    def __call__(self, req, timeout=None):
        form = None
        if req.data is not None:
            from urllib.parse import parse_qs
            form = {k: v[0] for k, v in parse_qs(req.data.decode()).items()}
        self.calls.append((req.full_url, form, req.get_header("Authorization")))
        if req.full_url.endswith("/oidc/token"):
            if isinstance(self.token, Exception):
                raise self.token
            return Resp(self.token or {"access_token": "acc-1", "refresh_token": "ref-2", "expires_in": 3600})
        if self.api_error:
            raise self.api_error
        return Resp(self.devices, LIMITS)

    def urls(self):
        return [c[0] for c in self.calls]
