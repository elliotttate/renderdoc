"""Nonblocking capture and replay helpers (§2 of the roadmap).

A small wrapper around `cap.OpenCapture(ReplayOptions(), ...)` that surfaces
any "incompatibility" warnings produced by the replay layer as structured
data instead of interactive modals. RenderDoc's existing ReplayOptions
already supports some of this; this module just gives us a single
machine-readable entry point.

Usage:
    python -m util.automation.nonblocking <capture.rdc>           # report only
    python -m util.automation.nonblocking <capture.rdc> --json    # JSON
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def report(capture_path: str) -> dict:
    cap = rd.OpenCaptureFile()
    res = cap.OpenFile(capture_path, "", None)
    if res != rd.ResultCode.Succeeded:
        return {"ok": False, "error": f"OpenFile failed: {res}"}

    info = {"ok": True, "path": os.path.abspath(capture_path)}

    info["localReplaySupport"] = bool(cap.LocalReplaySupport())
    info["recordedMachineIdent"] = str(cap.RecordedMachineIdent()) if hasattr(cap, "RecordedMachineIdent") else None
    try:
        info["thumbnail"] = bool(cap.HasThumbnail()) if hasattr(cap, "HasThumbnail") else None
    except Exception:
        info["thumbnail"] = None

    # Try to open with a "permissive" ReplayOptions; record any warnings.
    opts = rd.ReplayOptions()
    # Available ReplayOptions fields depend on the build; set what's present.
    if hasattr(opts, "apiValidation"):
        opts.apiValidation = False
    if hasattr(opts, "forceGPUVendor"):
        opts.forceGPUVendor = rd.GPUVendor.Unknown
    if hasattr(opts, "optimisation"):
        opts.optimisation = rd.ReplayOptimisationLevel.Fastest

    result, controller = cap.OpenCapture(opts, None)
    info["openCaptureResult"] = str(result).split(".")[-1] if hasattr(result, "code") is False else str(result.code).split(".")[-1]
    if result == rd.ResultCode.Succeeded or (hasattr(result, "code") and result.code == rd.ResultCode.Succeeded):
        debugs = controller.GetDebugMessages()
        info["debugMessageCount"] = len(debugs)
        info["debugMessages"] = [
            {
                "eventId": int(m.eventId) if hasattr(m, "eventId") else None,
                "category": str(m.category).split(".")[-1],
                "severity": str(m.severity).split(".")[-1],
                "source": str(m.source).split(".")[-1] if hasattr(m, "source") else None,
                "description": str(m.description),
            }
            for m in debugs[:200]
        ]
        controller.Shutdown()
    cap.Shutdown()
    return info


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Nonblocking capture compatibility report.")
    p.add_argument("capture")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        r = report(args.capture)
    finally:
        rd.ShutdownReplay()
    text = json.dumps(r, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
