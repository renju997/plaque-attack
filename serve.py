#!/usr/bin/env python3
"""Local dev server for Plaque Attack: serves the game statically and mirrors
api/game.py + api/leaderboard.py over plain http.server (in-memory sessions,
a local scores.jsonl file instead of Redis) -- same dual-implementation
pattern as Football Fever's serve.py, sharing the one game_engine.py module
so game logic never drifts between local dev and the deployed Vercel
functions.

Usage: python3 serve.py [port]   (default 8080)
"""
import sys
import os
import json
import time
import secrets
import threading
import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import game_engine

ROOT = os.path.dirname(os.path.abspath(__file__))
SCORES_FILE = os.path.join(ROOT, "scores.jsonl")
SESSION_TTL = 900

_lock = threading.Lock()
_sessions = {}  # sid -> {"seed": int, "createdAt": float}


def _prune_sessions():
    now = time.time()
    for sid in [sid for sid, s in _sessions.items() if now - s["createdAt"] > SESSION_TTL]:
        del _sessions[sid]


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/leaderboard"):
            return self._leaderboard()
        if self.path == "/leaderboard" or self.path.startswith("/leaderboard?"):
            self.path = "/leaderboard.html"  # mirrors vercel.json's rewrite locally
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/game"):
            return self._game()
        self.send_response(404)
        self.end_headers()

    def _game(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        action = payload.get("action")
        if action == "start":
            seed = secrets.randbelow(2 ** 32)
            sid = secrets.token_urlsafe(16)
            with _lock:
                _prune_sessions()
                _sessions[sid] = {"seed": seed, "createdAt": time.time()}
            return self._json(200, {"sessionId": sid, "seed": seed})

        if action == "finish":
            sid = payload.get("sessionId")
            taps = payload.get("taps")
            name = str(payload.get("name") or "Anonymous")[:24].strip() or "Anonymous"
            school = str(payload.get("school") or "")[:40].strip()
            try:
                age = max(1, min(120, int(payload.get("age"))))
            except (TypeError, ValueError):
                age = None
            if not sid or not isinstance(taps, list):
                return self._json(400, {"error": "bad request"})
            with _lock:
                session = _sessions.pop(sid, None)
            if not session:
                return self._json(409, {"error": "session expired or already finished"})

            result = game_engine.run_session(session["seed"], taps)
            record = {
                "name": name, "school": school, "age": age,
                "score": result["score"], "hits": result["hits"],
                "playedAt": datetime.datetime.utcnow().isoformat() + "Z",
            }
            with _lock:
                with open(SCORES_FILE, "a") as f:
                    f.write(json.dumps(record) + "\n")
            return self._json(200, {"score": result["score"], "hits": result["hits"]})

        return self._json(400, {"error": "unknown action"})

    def _leaderboard(self):
        rows = []
        if os.path.exists(SCORES_FILE):
            with open(SCORES_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        rows.sort(key=lambda r: r.get("score", 0), reverse=True)
        return self._json(200, rows[:20])

    def _json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # keep local dev output quiet


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    os.chdir(ROOT)
    server = ThreadingHTTPServer(("", port), Handler)
    print("Plaque Attack dev server: http://localhost:%d/plaque-attack.html" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
