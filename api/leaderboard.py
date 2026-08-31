"""Vercel serverless function: GET /api/leaderboard -- gated top scores.

Requires HTTP Basic credentials matching ADMIN_EMAIL/ADMIN_PASSWORD (set as
Vercel env vars -- no fallback default is baked in here, since this repo is
public and a hardcoded value would be exposed in source). leaderboard.html
prompts for email+password and sends them as an `Authorization: Basic`
header on every request; nothing is stored server-side across requests.

Submitting a score is unaffected -- that only ever happens through the
validated POST /api/game "finish" action, unrelated to this gate.

Env: KV_REST_API_URL/TOKEN or UPSTASH_REDIS_REST_URL/TOKEN, ADMIN_EMAIL,
ADMIN_PASSWORD.
"""
from http.server import BaseHTTPRequestHandler
import os
import json
import base64
import hmac
import urllib.parse
import urllib.request

SCORES_KEY = "pa:scores"
DEFAULT_LIMIT = 20


def _redis(command):
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        raise RuntimeError("no redis configured")
    req = urllib.request.Request(
        url.rstrip("/"),
        data=json.dumps(command).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


class handler(BaseHTTPRequestHandler):
    def _authed(self):
        expected_email = os.environ.get("ADMIN_EMAIL")
        expected_password = os.environ.get("ADMIN_PASSWORD")
        if not expected_email or not expected_password:
            return False  # fail closed if the gate isn't configured

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            email, _, password = decoded.partition(":")
        except Exception:
            return False

        return (
            hmac.compare_digest(email, expected_email)
            and hmac.compare_digest(password, expected_password)
        )

    def do_GET(self):
        if not self._authed():
            # No WWW-Authenticate header here on purpose: that header on a 401
            # makes browsers intercept the response and pop up their OWN native
            # login prompt before JS ever sees it (fetch() just hangs waiting
            # on a dialog nothing can dismiss) -- defeating the custom login
            # form in leaderboard.html, which handles 401s itself via JS.
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            body = json.dumps({"error": "unauthorized"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            limit = max(1, min(100, int((q.get("limit") or [DEFAULT_LIMIT])[0])))
        except ValueError:
            limit = DEFAULT_LIMIT

        try:
            res = _redis(["LRANGE", SCORES_KEY, "0", "-1"])
            rows = []
            for s in (res.get("result") or []):
                try:
                    rows.append(json.loads(s))
                except (ValueError, TypeError):
                    pass
        except Exception:
            return self._json(500, {"error": "store unavailable"})

        rows.sort(key=lambda r: r.get("score", 0), reverse=True)
        return self._json(200, rows[:limit])

    def _json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
