"""Local automation server (§15 of the roadmap).

Exposes the util/automation Python tools over a small HTTP JSON-RPC service
so external tools (UEVR, the user's MCP server, jq pipelines) can drive
RenderDoc replay without re-launching it per query.

Endpoints:
  GET  /healthz
  POST /open_capture       {"path": "...", "session_id": "optional"}
  POST /close_capture      {"session_id": "..."}
  POST /state_at_event     {"session_id": "...", "event": 16042}
  POST /index_capture      {"path": "...", "out": "..."}
  POST /resource_lineage   {"session_id": "...", "resource": "ResourceId::N", "before": 16042}
  POST /pixel_lineage      {"session_id": "...", "x": 900, "y": 250, "event": 16042}
  POST /eye_classify       {"session_id": "...", "mode": "auto"}
  POST /diff_captures      {"before": "<dir>", "after": "<dir>"}
  POST /uevr_ingest        {"index_dir": "...", "uevr_dir": "..."}
  POST /run_probe          {"session_id": "...", "kind": "swap"|"magenta_ps", ...}

Session model: open_capture returns a session_id; subsequent calls reuse the
same controller. A single process can host one session at a time (RenderDoc
replay is not multi-threaded against the same controller).

Usage:
    python -m util.automation.automation_server --host 127.0.0.1 --port 7745
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import (
        capture_diff,
        eye_classifier,
        index_capture as autom_index,
        pixel_lineage as autom_pixel,
        replay_probe,
        resource_lineage as autom_reslineage,
        uevr_ingest,
    )
else:
    from . import (
        _lib,
        capture_diff,
        eye_classifier,
        index_capture as autom_index,
        pixel_lineage as autom_pixel,
        replay_probe,
        resource_lineage as autom_reslineage,
        uevr_ingest,
    )

import renderdoc as rd  # noqa: E402


# ----- Session state --------------------------------------------------------


class SessionRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict = {}

    def open(self, path: str, session_id=None) -> dict:
        sid = session_id or uuid.uuid4().hex
        with self._lock:
            if sid in self._sessions:
                return {"error": "session_id already in use", "session_id": sid}
            cap, controller = _lib.open_capture(path)
            self._sessions[sid] = {"path": os.path.abspath(path), "cap": cap, "controller": controller}
        return {"session_id": sid, "path": os.path.abspath(path)}

    def close(self, sid: str) -> dict:
        with self._lock:
            entry = self._sessions.pop(sid, None)
        if entry is None:
            return {"error": "no such session"}
        try:
            entry["controller"].Shutdown()
            entry["cap"].Shutdown()
        except Exception as exc:
            return {"closed": True, "warning": str(exc)}
        return {"closed": True}

    def controller(self, sid: str):
        with self._lock:
            entry = self._sessions.get(sid)
        if entry is None:
            raise KeyError("no such session")
        return entry["controller"]

    def shutdown_all(self):
        with self._lock:
            sids = list(self._sessions.keys())
        for s in sids:
            self.close(s)


REGISTRY = SessionRegistry()


# ----- Handler --------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # quiet stderr
        return

    def _send_json(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "unknown endpoint", "path": self.path})

    def do_POST(self):
        try:
            req = self._read_json()
        except Exception as exc:
            self._send_json(400, {"error": "invalid JSON", "detail": str(exc)})
            return
        route = self.path.rstrip("/")
        try:
            handler = ROUTES.get(route)
            if handler is None:
                self._send_json(404, {"error": "unknown endpoint", "path": route})
                return
            result = handler(req)
            self._send_json(200, result)
        except KeyError as exc:
            self._send_json(404, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc), "type": type(exc).__name__})


# ----- Route implementations ------------------------------------------------


def _open_capture(req):
    path = req["path"]
    return REGISTRY.open(path, req.get("session_id"))


def _close_capture(req):
    return REGISTRY.close(req["session_id"])


def _state_at_event(req):
    c = REGISTRY.controller(req["session_id"])
    return _lib.collect_state_at_event(c, int(req["event"]))


def _index_capture(req):
    autom_index.index_capture(req["path"], req["out"])
    return {"ok": True, "out": os.path.abspath(req["out"])}


def _resource_lineage(req):
    c = REGISTRY.controller(req["session_id"])
    # Re-resolve resource via the controller already held by the session
    ref = req["resource"]
    rid = None
    name = None
    for r in c.GetResources():
        if str(r.resourceId) == ref or str(r.name) == ref:
            rid = r.resourceId
            name = str(r.name)
            break
    if rid is None:
        raise ValueError(f"resource not found: {ref}")
    usages = c.GetUsage(rid)
    before = req.get("before")
    rows = []
    last_writer = None
    for u in usages:
        kind = str(u.usage).split(".")[-1]
        row = {"eventId": int(u.eventId), "usage": kind, "isWrite": kind in autom_reslineage.WRITE_USAGES}
        rows.append(row)
        if before is not None and int(u.eventId) >= int(before):
            continue
        if row["isWrite"]:
            last_writer = int(u.eventId)
    return {"resource": str(rid), "name": name, "events": rows, "lastWriterBefore": last_writer}


def _pixel_lineage(req):
    # PixelHistory needs its own controller lifecycle; spin a fresh one to be safe.
    # If session_id is given, reuse that session's path so we replay the same capture.
    path = None
    if req.get("session_id"):
        with REGISTRY._lock:
            entry = REGISTRY._sessions.get(req["session_id"])
            if entry:
                path = entry["path"]
    if path is None:
        path = req["path"]
    return autom_pixel.pixel_lineage(path, int(req["x"]), int(req["y"]), req.get("event"))


def _eye_classify(req):
    path = None
    if req.get("session_id"):
        with REGISTRY._lock:
            entry = REGISTRY._sessions.get(req["session_id"])
            if entry:
                path = entry["path"]
    if path is None:
        path = req["path"]
    return eye_classifier.classify_capture(path, {"mode": req.get("mode", "auto")})


def _diff_captures(req):
    return capture_diff.diff_captures(req["before"], req["after"])


def _uevr_ingest(req):
    return uevr_ingest.ingest(req["index_dir"], req["uevr_dir"])


def _run_probe(req):
    sid = req["session_id"]
    with REGISTRY._lock:
        entry = REGISTRY._sessions[sid]
    path = entry["path"]
    kind = req["kind"]
    with replay_probe.ProbeSession(path) as s:
        roi = req.get("roi", [0, 0, 64, 64])
        before = s.sample_roi(int(req["event"]), *roi)
        if kind == "swap":
            s.swap_resource(req["orig"], req["repl"])
        elif kind == "magenta_ps":
            r = s.force_magenta_ps(req["shader"])
            if not r.get("ok"):
                return {"error": r.get("errors")}
        else:
            return {"error": f"unknown probe kind: {kind}"}
        after = s.sample_roi(int(req["event"]), *roi)
    return {"before": before, "after": after}


ROUTES = {
    "/open_capture": _open_capture,
    "/close_capture": _close_capture,
    "/state_at_event": _state_at_event,
    "/index_capture": _index_capture,
    "/resource_lineage": _resource_lineage,
    "/pixel_lineage": _pixel_lineage,
    "/eye_classify": _eye_classify,
    "/diff_captures": _diff_captures,
    "/uevr_ingest": _uevr_ingest,
    "/run_probe": _run_probe,
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Local automation server for RenderDoc.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7745)
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"automation_server listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        REGISTRY.shutdown_all()
        rd.ShutdownReplay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
