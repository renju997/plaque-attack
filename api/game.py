"""Vercel serverless function: POST /api/game -- server-authoritative Plaque Attack.

The browser plays a round locally off a server-issued seed (so spawn timing
has zero added network latency, unlike a per-shot round trip), then submits
its raw tap log at the end. THIS endpoint replays that seed through
game_engine.run_session() against the submitted taps and writes ITS OWN tally
to the leaderboard -- editing the front end's displayed score can't change
what gets stored. Mirrors Football Fever's api/game.py pattern (shared
engine module, Redis-backed session, start/finish actions).

Body: {"action":"start"} | {"action":"finish","sessionId","taps","name","age"}.
Sessions live in Redis (15-min TTL) alongside the leaderboard.

Env: KV_REST_API_URL/TOKEN or UPSTASH_REDIS_REST_URL/TOKEN (same Upstash
account as Football Fever -- just a different key prefix, "pa:" not "pf:").
"""
from http.server import BaseHTTPRequestHandler
import os
import sys
import json
import time
import secrets
import datetime
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import game_engine  # noqa: E402

SCORES_KEY = "pa:scores"
SESSION_PREFIX = "pa:game:"
SESSION_TTL = 900  # seconds -- a round must finish within 15 minutes


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
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        action = payload.get("action")
        if action == "start":
            return self._start()
        if action == "finish":
            return self._finish(payload)
        return self._json(400, {"error": "unknown action"})

    def _start(self):
        seed = secrets.randbelow(2 ** 32)
        sid = secrets.token_urlsafe(16)
        session = {"seed": seed, "createdAt": time.time()}
        try:
            _redis(["SET", SESSION_PREFIX + sid, json.dumps(session), "EX", str(SESSION_TTL)])
        except Exception:
            return self._json(500, {"error": "store unavailable"})
        return self._json(200, {"sessionId": sid, "seed": seed})

    def _finish(self, payload):
        sid = payload.get("sessionId")
        taps = payload.get("taps")
        name = str(payload.get("name") or "Anonymous")[:24].strip() or "Anonymous"
        try:
            age = max(1, min(120, int(payload.get("age"))))
        except (TypeError, ValueError):
            age = None

        if not sid or not isinstance(taps, list):
            return self._json(400, {"error": "bad request"})

        try:
            raw = _redis(["GET", SESSION_PREFIX + sid])
        except Exception:
            return self._json(500, {"error": "store unavailable"})
        session_json = raw.get("result")
        if not session_json:
            return self._json(409, {"error": "session expired or already finished"})
        session = json.loads(session_json)

        result = game_engine.run_session(session["seed"], taps)

        record = {
            "name": name,
            "age": age,
            "score": result["score"],
            "hits": result["hits"],
            "playedAt": datetime.datetime.utcnow().isoformat() + "Z",
        }
        try:
            _redis(["RPUSH", SCORES_KEY, json.dumps(record)])
            _redis(["DEL", SESSION_PREFIX + sid])
        except Exception:
            return self._json(500, {"error": "store unavailable"})

        return self._json(200, {"score": result["score"], "hits": result["hits"]})

    def _json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
