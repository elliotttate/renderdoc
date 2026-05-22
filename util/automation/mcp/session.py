"""Persistent capture-session manager for the MCP server.

Holds open RenderDoc replay-controller handles across MCP calls so the
LLM doesn't pay the 5-15s open-capture cost on every query.
"""

import os
import sys
import threading
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from automation import _lib  # type: ignore
else:
    from .. import _lib


class CaptureSession:
    def __init__(self, path: str):
        self.id = uuid.uuid4().hex[:12]
        self.path = os.path.abspath(path)
        self.cap, self.controller = _lib.open_capture(path)
        self.lock = threading.RLock()

    def close(self):
        try:
            self.controller.Shutdown()
        except Exception:
            pass
        try:
            self.cap.Shutdown()
        except Exception:
            pass


class SessionManager:
    def __init__(self):
        self._sessions = {}  # id -> CaptureSession
        self._by_path = {}   # abs path -> id
        self._lock = threading.RLock()

    def open(self, path: str) -> str:
        with self._lock:
            absp = os.path.abspath(path)
            existing = self._by_path.get(absp)
            if existing:
                return existing
            sess = CaptureSession(path)
            self._sessions[sess.id] = sess
            self._by_path[absp] = sess.id
            return sess.id

    def get(self, session_id: str) -> CaptureSession:
        with self._lock:
            return self._sessions.get(session_id)

    def get_or_open(self, session_id_or_path: str) -> CaptureSession:
        sess = self.get(session_id_or_path)
        if sess: return sess
        if os.path.isfile(session_id_or_path):
            sid = self.open(session_id_or_path)
            return self._sessions[sid]
        raise ValueError(f"Unknown session_id and not a path: {session_id_or_path}")

    def list(self) -> list:
        with self._lock:
            return [
                {"id": s.id, "path": s.path}
                for s in self._sessions.values()
            ]

    def close(self, session_id: str) -> bool:
        with self._lock:
            sess = self._sessions.pop(session_id, None)
            if not sess: return False
            self._by_path.pop(sess.path, None)
            sess.close()
            return True

    def close_all(self):
        with self._lock:
            ids = list(self._sessions.keys())
        for i in ids:
            self.close(i)


# Global, lazy
_global_manager = None
_manager_lock = threading.Lock()


def manager() -> SessionManager:
    global _global_manager
    with _manager_lock:
        if _global_manager is None:
            _global_manager = SessionManager()
        return _global_manager
