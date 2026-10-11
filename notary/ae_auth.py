"""Exchange the Agent Engine service account for a short-lived access token (cached, refreshed before expiry)."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()
_cache: dict = {"token": None, "exp": 0.0}


def access_token() -> str:
    if _cache["token"] and time.time() < _cache["exp"] - 120:
        return _cache["token"]
    base = os.environ.get("AE_BASE_URL", "https://agentengine.mongodb.com")
    basic = base64.b64encode(f"{os.environ['AE_CLIENT_ID']}:{os.environ['AE_CLIENT_SECRET']}".encode()).decode()
    req = urllib.request.Request(
        f"{base}/api/v1/oauth/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    _cache["token"] = body["access_token"]
    _cache["exp"] = time.time() + float(body.get("expires_in", 3600))
    return _cache["token"]
