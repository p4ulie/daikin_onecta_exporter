"""One-time OAuth login: prints the authorize URL, takes the pasted redirect URL, prints only the seed JSON."""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

from . import onecta


def main(opener=urllib.request.urlopen) -> None:
    cid, secret = os.environ.get("DAIKIN_CLIENT_ID"), os.environ.get("DAIKIN_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("DAIKIN_CLIENT_ID and DAIKIN_CLIENT_SECRET are required")
    redirect = os.environ.get("DAIKIN_REDIRECT_URI", "https://localhost/")
    url = onecta.AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": cid, "redirect_uri": redirect, "scope": onecta.SCOPE})
    # stdout is for the seed JSON only, so the owner can pipe it; everything else goes to stderr.
    print(f"Open this link, log in with your Onecta account and agree:\n\n{url}\n", file=sys.stderr)
    sys.stderr.write("Paste the whole address the browser ends up on (https://localhost/?code=...): ")
    sys.stderr.flush()
    pasted = sys.stdin.readline().strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [""])[0]
    if not code:
        sys.exit("no code= in the pasted address")
    try:
        body = onecta.token_request(opener, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                             "client_id": cid, "client_secret": secret})
        refresh = body["refresh_token"]
    except (onecta.OnectaError, KeyError) as e:
        sys.exit(f"login failed ({e}); the code works once and for a few minutes, run the helper again")
    print(json.dumps({"refresh_token": refresh, "authorized_at": int(time.time())}))


if __name__ == "__main__":
    main()
