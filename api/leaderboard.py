"""Vercel serverless function: GET /api/leaderboard -- public top scores.

Unlike Football Fever's admin-gated /api/scores (built for moderation),
Plaque Attack's leaderboard is meant to be shown to players in-game, so
reading it is intentionally open -- there's no way to write to it except
through the validated POST /api/game "finish" action, so an open read isn't
a scoring risk.

Env: KV_REST_API_URL/TOKEN or UPSTASH_REDIS_REST_URL/TOKEN.
"""
from http.server import BaseHTTPRequestHandler
import os
import json
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
    def do_GET(self):
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
