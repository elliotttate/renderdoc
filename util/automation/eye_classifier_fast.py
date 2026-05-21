"""Fast eye classifier — no SetFrameEvent per action.

The default eye_classifier.py walks every draw/dispatch and calls
controller.SetFrameEvent + GetD3D12PipelineState to read the bound
viewport. On a 1.4 GB SN2 capture with ~2200 actions, this takes
5-10 minutes.

This module skips SetFrameEvent entirely and tracks viewports by
replaying the chunk stream forward: every RSSetViewports updates the
current viewport, every Draw/Dispatch consumes the current viewport.

Trade-off: this only sees per-command-list viewport state. For captures
that bind viewports via bundle execute or other indirection paths, the
slower classifier in eye_classifier.py is more accurate.

For SN2's straightforward main-eye SBS flow, this module is ~50x faster
and produces equivalent results.

Usage::

    python -m util.automation.eye_classifier_fast <cap.rdc> [--out file]
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


VIEWPORT_CHUNK_NAMES = {
    "ID3D12GraphicsCommandList::RSSetViewports",
}

DRAW_CHUNK_NAMES = {
    "ID3D12GraphicsCommandList::DrawInstanced",
    "ID3D12GraphicsCommandList::DrawIndexedInstanced",
    "ID3D12GraphicsCommandList::Dispatch",
    "ID3D12GraphicsCommandList::ExecuteIndirect",
    "ID3D12GraphicsCommandList6::DispatchMesh",
    "ID3D12GraphicsCommandList4::DispatchRays",
}


def _read_first_viewport(chunk) -> Optional[Tuple[float, float, float, float]]:
    """Pull the first viewport from a serialised RSSetViewports chunk."""
    arr = chunk.FindChild("pViewports") if hasattr(chunk, "FindChild") else None
    if arr is None or arr.NumChildren() == 0:
        return None
    v = arr.GetChild(0)

    def _f(name, default=0.0):
        c = v.FindChild(name)
        if c is None:
            return default
        try:
            return float(c.AsFloat())
        except Exception:
            try:
                return float(c.data.basic.d)
            except Exception:
                return default

    return (_f("TopLeftX"), _f("TopLeftY"), _f("Width"), _f("Height"))


def _read_event_id(chunk) -> Optional[int]:
    """Pull the eventId from chunk metadata, if available."""
    try:
        return int(chunk.metadata.eventId)
    except Exception:
        return None


def classify(capture_path: str, force_full_w: int = 0, force_full_h: int = 0) -> Dict[str, Any]:
    cap, controller = _lib.open_capture(capture_path)
    try:
        sdfile = controller.GetStructuredFile()
        try:
            n = sdfile.chunks.size()
        except Exception:
            n = len(sdfile.chunks)

        # First pass — find the most common scene-RT shape via draw RTV bindings
        # (faster than SetFrameEvent). For the SN2 case we assume 1280x720 if
        # the user passed a hint.
        if force_full_w and force_full_h:
            inferred_w, inferred_h = int(force_full_w), int(force_full_h)
        else:
            # Use the existing main-RT inference (still requires SetFrameEvent
            # but only once, not per-action).
            from automation import eye_classifier as ec  # type: ignore
            textures_by_id = {str(t.resourceId): t for t in controller.GetTextures()}
            inferred_w, inferred_h = ec._infer_main_rt(controller, textures_by_id)

        # Second pass — chunk stream walk, tracking the current viewport.
        current_vp: Optional[Tuple[float, float, float, float]] = None
        rows: List[Dict[str, Any]] = []
        per_eye = {"left": 0, "right": 0, "unknown": 0}
        for i in range(n):
            chunk = sdfile.chunks[i]
            name = str(chunk.name)
            if name in VIEWPORT_CHUNK_NAMES:
                vp = _read_first_viewport(chunk)
                if vp is not None:
                    current_vp = vp
                continue
            if name not in DRAW_CHUNK_NAMES:
                continue

            eid = _read_event_id(chunk)
            if eid is None:
                continue

            eye = "unknown"
            confidence = 0.0
            reason = ""

            if current_vp is not None and inferred_w >= 2 * inferred_h and inferred_w > 0:
                cx = current_vp[0] + current_vp[2] / 2.0
                half = inferred_w / 2.0
                if cx < half * 0.85:
                    eye = "left"
                    confidence = 0.9
                    reason = f"fast: vp x={current_vp[0]:.0f} of {inferred_w}"
                elif cx > half * 1.15:
                    eye = "right"
                    confidence = 0.9
                    reason = f"fast: vp x={current_vp[0]:.0f} of {inferred_w}"

            per_eye[eye] = per_eye.get(eye, 0) + 1
            rows.append({
                "eventId": eid,
                "eye": eye,
                "reason": reason,
                "confidence": confidence,
                "viewport": list(current_vp) if current_vp else None,
            })

        return {
            "capture": capture_path,
            "fullSize": [inferred_w, inferred_h],
            "perEyeCounts": per_eye,
            "events": rows,
            "mode": "fast-chunkwalk",
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fast eye classifier (chunk-walk, no SetFrameEvent).")
    p.add_argument("capture")
    p.add_argument("--width", type=int, default=0, help="Force main-RT width (skip inference)")
    p.add_argument("--height", type=int, default=0, help="Force main-RT height (skip inference)")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = classify(args.capture, force_full_w=args.width, force_full_h=args.height)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(json.dumps(out["perEyeCounts"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
