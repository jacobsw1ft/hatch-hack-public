"""
Single Vercel entrypoint (WSGI Flask app) for the Hatch controller backend.

Vercel's Python runtime builds the repo as one application with a single
entrypoint, so all endpoints live here and route by path. Declared in
pyproject.toml as  [tool.vercel] entrypoint = "api.index:app".

Endpoints (all except /api/verify require the X-Pin header):
    POST /api/verify        {pin}                       -> {ok}
    GET  /api/alarms                                    -> {device, alarms[]}
    POST /api/alarms        {action, ...}               -> {ok, alarms[]}
    POST /api/nightlight    {on}                        -> {ok, on}
    POST /api/sound         {on, sound, volume}         -> {ok, on}
"""

import os
import sys

# Make the sibling _lib module importable regardless of how Vercel loads this.
sys.path.insert(0, os.path.dirname(__file__))
import _lib  # noqa: E402

from flask import Flask, request, jsonify  # noqa: E402

app = Flask(__name__)


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = _lib.frontend_origin()
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Pin"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return ("", 204)


def _pin_ok():
    return _lib.check_pin(request.headers.get("X-Pin"))


def _guard(fn):
    try:
        return jsonify(fn())
    except Exception as e:  # surface a readable message to the frontend
        return jsonify(error=str(e)), 500


@app.get("/")
def health():
    return jsonify(ok=True, service="hatch-hack")


@app.post("/api/verify")
def verify():
    pin = (request.get_json(silent=True) or {}).get("pin")
    return jsonify(ok=_lib.check_pin(pin))


@app.get("/api/alarms")
def alarms_list():
    if not _pin_ok():
        return jsonify(error="invalid pin"), 401
    return _guard(_lib.op_list_alarms)


@app.post("/api/alarms")
def alarms_mutate():
    if not _pin_ok():
        return jsonify(error="invalid pin"), 401
    body = request.get_json(silent=True) or {}
    return _guard(lambda: _lib.op_mutate_alarm(body))


@app.post("/api/nightlight")
def nightlight():
    if not _pin_ok():
        return jsonify(error="invalid pin"), 401
    body = request.get_json(silent=True) or {}
    return _guard(lambda: _lib.set_nightlight(bool(body.get("on", True))))


@app.post("/api/sound")
def sound():
    if not _pin_ok():
        return jsonify(error="invalid pin"), 401
    body = request.get_json(silent=True) or {}
    return _guard(lambda: _lib.set_sound_now(
        bool(body.get("on", True)), body.get("sound"), int(body.get("volume", 40))))
