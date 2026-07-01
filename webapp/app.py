"""Flask API + website for Mini-Circuits solid-state switches (real hardware).

Run (from the repo root):
    pip install -r requirements.txt
    python3 webapp/app.py
    # open http://127.0.0.1:5000

Needs the switch plugged in and USB permission (root, or the udev rule in
packaging/). When the hardware is unreachable the API returns HTTP 503 with the
real reason — nothing is simulated.

JSON API
--------
GET  /api/health                      -> {ok, connected, units, error}
POST /api/reconnect                   -> {units:[...]}  (or 503)
GET  /api/units                       -> {units:[{serial,model,firmware}], channels, ports}
GET  /api/states                      -> {serial:{A,B}, ...}
GET  /api/units/<serial>/state        -> {A,B}
POST /api/units/<serial>/state        body {A?,B?}        -> {A,B}
POST /api/set_all                     body {a?,b?}        -> {serial:{A,B}, ...}
POST /api/scpi                        body {serial,command} -> {reply}
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, render_template, request

from backend import CHANNELS, PORTS, HardwareUnavailable, SwitchService

app = Flask(__name__)
switches = SwitchService()

# Dev server: never let the browser cache app.js / style.css, so edits show up
# on a plain reload instead of a forced hard-refresh.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def _no_store_static(resp):
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.context_processor
def _asset_versions():
    """Expose `static_v('app.js')` -> '/static/app.js?v=<mtime>' for cache-busting."""
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    def static_v(filename):
        try:
            v = int(os.path.getmtime(os.path.join(static_dir, filename)))
        except OSError:
            v = 0
        return f"/static/{filename}?v={v}"

    return {"static_v": static_v}


def _valid_port(value):
    port = int(value)
    if port not in PORTS:
        raise ValueError(f"port must be one of {list(PORTS)}")
    return port


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True, **switches.health())


@app.post("/api/reconnect")
def reconnect():
    try:
        info = switches.reconnect()
        return jsonify(units=list(info.values()))
    except HardwareUnavailable as exc:
        return jsonify(error=str(exc)), 503


@app.get("/api/units")
def units():
    try:
        info = switches.info()
    except HardwareUnavailable as exc:
        return jsonify(error=str(exc)), 503
    return jsonify(units=list(info.values()), channels=list(CHANNELS), ports=list(PORTS))


@app.get("/api/states")
def states():
    try:
        return jsonify(switches.states())
    except HardwareUnavailable as exc:
        return jsonify(error=str(exc)), 503


@app.get("/api/units/<serial>/state")
def get_unit_state(serial):
    try:
        all_states = switches.states()
    except HardwareUnavailable as exc:
        return jsonify(error=str(exc)), 503
    if serial not in all_states:
        return jsonify(error=f"unknown serial {serial!r}"), 404
    return jsonify(all_states[serial])


@app.post("/api/units/<serial>/state")
def set_unit_state(serial):
    body = request.get_json(silent=True) or {}
    try:
        a = _valid_port(body["A"]) if body.get("A") is not None else None
        b = _valid_port(body["B"]) if body.get("B") is not None else None
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    if a is None and b is None:
        return jsonify(error="provide at least one of A or B"), 400
    try:
        if serial not in switches.serials():
            return jsonify(error=f"unknown serial {serial!r}"), 404
        return jsonify(switches.set_state(serial, a=a, b=b))
    except HardwareUnavailable as exc:
        return jsonify(error=str(exc)), 503


@app.post("/api/set_all")
def set_all():
    body = request.get_json(silent=True) or {}
    try:
        a = _valid_port(body["a"]) if body.get("a") is not None else None
        b = _valid_port(body["b"]) if body.get("b") is not None else None
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    if a is None and b is None:
        return jsonify(error="provide at least one of a or b"), 400
    try:
        return jsonify(switches.set_all(a=a, b=b))
    except HardwareUnavailable as exc:
        return jsonify(error=str(exc)), 503


# ----- presets (saved routings, persisted to a JSON file on the host) ------- #
PRESETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")


def _load_presets():
    try:
        with open(PRESETS_FILE) as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_presets(data):
    with open(PRESETS_FILE, "w") as fh:
        json.dump(data, fh, indent=2)


def _presets_list(data):
    return [{"name": k, "states": v} for k, v in data.items()]


@app.get("/api/presets")
def get_presets():
    return jsonify(presets=_presets_list(_load_presets()))


@app.post("/api/presets")
def save_preset():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(error="preset name is required"), 400
    states = body.get("states")
    if states is None:                       # capture the live routing
        try:
            states = switches.states()
        except HardwareUnavailable as exc:
            return jsonify(error=str(exc)), 503
    data = _load_presets()
    data[name] = states
    _save_presets(data)
    return jsonify(presets=_presets_list(data))


@app.post("/api/presets/<name>/apply")
def apply_preset(name):
    data = _load_presets()
    if name not in data:
        return jsonify(error=f"unknown preset {name!r}"), 404
    try:
        return jsonify(switches.apply_mapping(data[name]))
    except HardwareUnavailable as exc:
        return jsonify(error=str(exc)), 503


@app.delete("/api/presets/<name>")
def delete_preset(name):
    data = _load_presets()
    data.pop(name, None)
    _save_presets(data)
    return jsonify(presets=_presets_list(data))


@app.post("/api/scpi")
def scpi():
    body = request.get_json(silent=True) or {}
    serial, command = body.get("serial"), body.get("command")
    if not serial or not command:
        return jsonify(error="body must include 'serial' and 'command'"), 400
    try:
        if serial not in switches.serials():
            return jsonify(error=f"unknown serial {serial!r}"), 404
        return jsonify(reply=switches.send_scpi(serial, command))
    except HardwareUnavailable as exc:
        return jsonify(error=str(exc)), 503


if __name__ == "__main__":
    st = switches.status()
    if st["connected"]:
        print(f"[mini-switch] connected to {st['units']} unit(s)")
    else:
        print(f"[mini-switch] no hardware yet: {st['error']}")
        print("[mini-switch] plug in the switch and use the Retry button / POST /api/reconnect")
    print("[mini-switch] serving on http://127.0.0.1:5000")
    # use_reloader=False so the USB handle isn't claimed twice by the reloader.
    app.run(host="127.0.0.1", port=5000, threaded=True, use_reloader=False)
