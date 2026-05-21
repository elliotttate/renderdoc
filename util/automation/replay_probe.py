"""Replay-time mutation probes (§16 of the roadmap).

Uses controller.ReplaceResource() and controller.BuildCustomShader() to mutate
state at replay time without modifying the live game, then re-samples a region
of interest to measure the effect.

Probes:
  - swap_resource: redirect a resource ID to another (e.g. right-eye t9 -> left-eye t9)
  - bind_neutral: bind a 1x1 white/black/magenta texture in place of a target
  - force_magenta_ps: replace a pixel shader with one that outputs magenta
  - reset: undo all replacements

This module intentionally keeps the controller alive across a probe -> measure
cycle so the workflow can be scripted as:

    with ProbeSession(capture) as s:
        roi_before = s.sample_roi(event_id, 100, 100, 200, 200)
        s.swap_resource("ResourceId(12345)", "ResourceId(54321)")
        roi_after = s.sample_roi(event_id, 100, 100, 200, 200)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


class ProbeSession:
    def __init__(self, capture_path: str):
        self.capture_path = capture_path
        self.cap = None
        self.controller = None
        self._replacements = []

    def __enter__(self):
        self.cap, self.controller = _lib.open_capture(self.capture_path)
        return self

    def __exit__(self, *exc):
        try:
            self.reset()
        finally:
            if self.controller is not None:
                self.controller.Shutdown()
            if self.cap is not None:
                self.cap.Shutdown()

    def _find_resource(self, ref):
        for r in self.controller.GetResources():
            if str(r.resourceId) == ref or str(r.name) == ref:
                return r.resourceId
        raise ValueError(f"resource not found: {ref}")

    def swap_resource(self, original_ref: str, replacement_ref: str):
        orig = self._find_resource(original_ref)
        repl = self._find_resource(replacement_ref)
        self.controller.ReplaceResource(orig, repl)
        self._replacements.append(orig)

    def force_magenta_ps(self, ps_resource_ref: str):
        """Replace `ps_resource_ref` with a custom HLSL pixel shader that outputs magenta."""
        orig = self._find_resource(ps_resource_ref)
        hlsl = (
            "float4 main(): SV_Target { return float4(1.0, 0.0, 1.0, 1.0); }\n"
        )
        replacement, errors = self.controller.BuildCustomShader(
            "main",
            rd.ShaderEncoding.HLSL,
            bytes(hlsl, "utf-8"),
            rd.ShaderCompileFlags(),
            rd.ShaderStage.Pixel,
        )
        if str(errors).strip():
            return {"ok": False, "errors": str(errors)}
        self.controller.ReplaceResource(orig, replacement)
        self._replacements.append(orig)
        return {"ok": True, "originalShader": str(orig), "replacement": str(replacement)}

    def reset(self):
        if self.controller is None:
            return
        for r in self._replacements:
            try:
                self.controller.RemoveReplacement(r)
            except Exception:
                pass
        self._replacements = []

    def sample_roi(self, event_id: int, x: int, y: int, w: int, h: int) -> dict:
        """Return per-channel min/max/mean for an RxC ROI of the first bound RT at event."""
        self.controller.SetFrameEvent(int(event_id), True)
        d3d12 = None
        try:
            d3d12 = self.controller.GetD3D12PipelineState()
        except Exception:
            pass
        if d3d12 is None or len(d3d12.outputMerger.renderTargets) == 0:
            return {"error": "no RT bound"}
        rt = d3d12.outputMerger.renderTargets[0]

        sub = rd.Subresource(int(rt.firstMip), int(rt.firstSlice), 0)
        raw = bytes(self.controller.GetTextureData(rt.resource, sub))

        # Look up RT size to do indexed sample
        tex = None
        for t in self.controller.GetTextures():
            if t.resourceId == rt.resource:
                tex = t
                break
        if tex is None:
            return {"error": "RT texture not found"}
        rw, rh = int(tex.width), int(tex.height)
        fmt_name = str(tex.format.Name()) if hasattr(tex.format, "Name") else str(tex.format)

        # Only sample formats we know how to decode cheaply.
        if fmt_name not in ("R8G8B8A8_UNORM", "B8G8R8A8_UNORM"):
            return {
                "rt": _lib.resource_id_str(rt.resource),
                "format": fmt_name,
                "note": "format not sampled; use external decoder",
                "size": [rw, rh],
            }
        bpp = 4
        vals = [[], [], [], []]
        for yy in range(y, min(y + h, rh)):
            for xx in range(x, min(x + w, rw)):
                off = (yy * rw + xx) * bpp
                if off + bpp > len(raw):
                    continue
                for c in range(4):
                    vals[c].append(raw[off + c] / 255.0)
        out = []
        for c in range(4):
            if not vals[c]:
                continue
            out.append(
                {
                    "channel": c,
                    "min": min(vals[c]),
                    "max": max(vals[c]),
                    "mean": sum(vals[c]) / len(vals[c]),
                }
            )
        return {
            "rt": _lib.resource_id_str(rt.resource),
            "roi": [x, y, w, h],
            "size": [rw, rh],
            "format": fmt_name,
            "perChannel": out,
        }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Replay-time mutation probes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pswap = sub.add_parser("swap")
    pswap.add_argument("capture")
    pswap.add_argument("--orig", required=True)
    pswap.add_argument("--repl", required=True)
    pswap.add_argument("--event", "-e", type=int, required=True)
    pswap.add_argument("--roi", nargs=4, type=int, default=(0, 0, 64, 64), help="x y w h")

    pmag = sub.add_parser("magenta-ps")
    pmag.add_argument("capture")
    pmag.add_argument("--shader", required=True, help="Pixel shader ResourceId or name")
    pmag.add_argument("--event", "-e", type=int, required=True)
    pmag.add_argument("--roi", nargs=4, type=int, default=(0, 0, 64, 64))

    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        with ProbeSession(args.capture) as s:
            before = s.sample_roi(args.event, *args.roi)
            if args.cmd == "swap":
                s.swap_resource(args.orig, args.repl)
            else:
                r = s.force_magenta_ps(args.shader)
                if not r["ok"]:
                    print(json.dumps(r, indent=2))
                    return 1
            after = s.sample_roi(args.event, *args.roi)
        print(json.dumps({"before": before, "after": after}, indent=2, ensure_ascii=False))
    finally:
        rd.ShutdownReplay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
