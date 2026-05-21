"""Smoke test for export_cpp.py - run via qrenderdoc --python.

Validates that export_cpp can walk a real D3D12 capture and emit a
compilable C++ project structure.

Usage:
    set RENDERDOC_EXPORT_CPP_CAPTURE=path\to\some.rdc
    set RENDERDOC_EXPORT_CPP_OUT=path\to\out_dir
    "<build>\qrenderdoc.exe" --python util\automation\_smoke_test_export_cpp.py

If env vars aren't set, this is a no-op log.

This file is for local validation; it doesn't run in CI.
"""
import os
import sys
import traceback

LOG = os.path.join(os.environ.get("TEMP", "."), "export_cpp_smoke.log")


def log(s):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass


with open(LOG, "w") as f:
    pass

log("START")
log(f"sys.version={sys.version}")

CAPTURE = os.environ.get("RENDERDOC_EXPORT_CPP_CAPTURE", "")
OUT = os.environ.get("RENDERDOC_EXPORT_CPP_OUT", os.path.join(os.environ.get("TEMP", "."), "export_cpp_smoke"))

if not CAPTURE or not os.path.exists(CAPTURE):
    log(f"RENDERDOC_EXPORT_CPP_CAPTURE not set or capture missing: {CAPTURE!r}")
    log("no-op exit")
    os._exit(0)

# Locate util/automation on sys.path. When run via qrenderdoc --python, the
# script's __file__ isn't defined, so we use an explicit RENDERDOC_REPO env
# var or hardcoded fallback.
repo = os.environ.get("RENDERDOC_REPO", "")
if not repo:
    # Best-effort: the script lives at <repo>/util/automation/_smoke_test_export_cpp.py
    here = os.environ.get("RENDERDOC_AUTOMATION_DIR")
    if here:
        repo = os.path.dirname(os.path.dirname(here))
if not repo or not os.path.isdir(os.path.join(repo, "util", "automation")):
    log(f"Could not locate repo root (set RENDERDOC_REPO). Trying CWD.")
    repo = os.getcwd()
sys.path.insert(0, repo)
log(f"repo={repo}")

try:
    log("importing export_cpp")
    from util.automation import export_cpp

    log(f"capture: {CAPTURE} ({os.path.getsize(CAPTURE)} bytes)")
    log(f"out: {OUT}")
    log("calling export()...")
    result = export_cpp.export(CAPTURE, OUT, write_blobs=True)
    log(f"RESULT: {result}")
    log("=== SUCCESS ===")
except Exception:
    log(traceback.format_exc())
    log("=== FAILED ===")

os._exit(0)
