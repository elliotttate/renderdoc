"""Event histogram tool — group actions by various keys, surface distributions.

Companion to quick_triage.py — same kind of analysis but parameterizable
for ad-hoc questions:
  * group_by="kind"       — same as quick_triage's actions_by_kind
  * group_by="pso"        — PSO ResourceId
  * group_by="ps_entry"   — pixel-shader entry name
  * group_by="cs_entry"   — compute-shader entry name
  * group_by="rt_shape"   — render-target dimensions
  * group_by="viewport_x" — viewport.x bucketed (for stereo detection)
  * group_by="rt_resource" — RT0 ResourceId
  * group_by="action_name" — action's GetName()
  * group_by="num_indices_bucket" — log-buckets of numIndices (0/1-10/11-100/...)

CLI:
    python util/automation/event_histogram.py <capture.rdc> --by pso
    python util/automation/event_histogram.py <capture.rdc> --by viewport_x --kind draw
    python util/automation/event_histogram.py <capture.rdc> --by rt_resource --json out.json
"""

import argparse
import json
import math
import os
import sys
from collections import Counter

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _action_kind(action):
    f = int(action.flags)
    if f & int(rd.ActionFlags.Drawcall):     return "draw"
    if f & int(rd.ActionFlags.Dispatch):     return "dispatch"
    if f & int(rd.ActionFlags.Copy):         return "copy"
    if f & int(rd.ActionFlags.Resolve):      return "resolve"
    if f & int(rd.ActionFlags.Clear):        return "clear"
    if f & int(rd.ActionFlags.GenMips):      return "genmips"
    if f & int(rd.ActionFlags.Present):      return "present"
    return "other"


def _num_indices_bucket(n):
    if n == 0: return "0"
    if n <= 3: return "1-3 (triangle)"
    if n <= 10: return "4-10"
    if n <= 100: return "11-100"
    if n <= 1000: return "101-1k"
    if n <= 10000: return "1k-10k"
    if n <= 100000: return "10k-100k"
    if n <= 1000000: return "100k-1M"
    return ">1M"


def histogram(capture_path: str, group_by: str = "kind",
              kind_filter: str = "all") -> Counter:
    cap, controller = _lib.open_capture(capture_path)
    counts = Counter()
    try:
        for a in _lib.walk_actions(controller):
            kind = _action_kind(a)
            if kind_filter != "all" and kind != kind_filter:
                continue
            key = None
            if group_by == "kind":
                key = kind
            elif group_by == "num_indices_bucket":
                key = _num_indices_bucket(int(a.numIndices))
            elif group_by == "action_name":
                try:
                    key = str(a.GetName(controller.GetStructuredFile()))
                except Exception: pass
            else:
                # need pipe state
                if kind not in ("draw", "dispatch"):
                    continue
                eid = int(a.eventId)
                try:
                    controller.SetFrameEvent(eid, True)
                    d3d12 = controller.GetD3D12PipelineState()
                    pipe = controller.GetPipelineState()
                except Exception:
                    continue
                if group_by == "pso":
                    try: key = _lib.resource_id_str(d3d12.pipelineResourceId)
                    except Exception: pass
                elif group_by == "rt_resource":
                    try:
                        rt = d3d12.outputMerger.renderTargets[0]
                        key = _lib.resource_id_str(rt.resource)
                    except Exception: pass
                elif group_by == "rt_shape":
                    try:
                        rt = d3d12.outputMerger.renderTargets[0]
                        rid = _lib.resource_id_str(rt.resource)
                        if rid:
                            for t in controller.GetTextures():
                                if _lib.resource_id_str(t.resourceId) == rid:
                                    key = f"{int(t.width)}x{int(t.height)}"
                                    break
                    except Exception: pass
                elif group_by == "viewport_x":
                    try:
                        key = str(int(d3d12.rasterizer.viewports[0].x))
                    except Exception: pass
                elif group_by in ("ps_entry", "cs_entry"):
                    stage = rd.ShaderStage.Pixel if group_by == "ps_entry" else rd.ShaderStage.Compute
                    try:
                        refl = pipe.GetShaderReflection(stage)
                        if refl and len(refl.rawBytes) > 0:
                            key = str(refl.entryPoint)
                    except Exception: pass
            if key is None:
                key = "<none>"
            counts[key] += 1
        return counts
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--by", default="kind",
                     choices=["kind", "pso", "ps_entry", "cs_entry", "rt_shape",
                              "viewport_x", "rt_resource", "action_name",
                              "num_indices_bucket"])
    ap.add_argument("--kind", default="all",
                     choices=["all", "draw", "dispatch", "copy", "resolve", "clear", "present"])
    ap.add_argument("--top", type=int, default=0,
                     help="only show top-N (0 = all)")
    ap.add_argument("--json", help="dump full histogram")
    args = ap.parse_args()

    h = histogram(args.capture, args.by, args.kind)
    items = h.most_common(args.top if args.top else None)

    total = sum(h.values())
    print(f"\nGroup by '{args.by}' (kind filter '{args.kind}'): {total} total events, {len(h)} unique groups\n")
    for k, n in items:
        bar = "█" * min(50, (n * 50 // (items[0][1] if items else 1)))
        print(f"  {n:>7}  {bar:50}  {k}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"group_by": args.by, "kind": args.kind,
                       "total": total, "unique": len(h),
                       "buckets": dict(items)}, f, indent=2, default=str)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
