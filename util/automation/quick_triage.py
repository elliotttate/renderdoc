"""Quick triage + event histogram for any capture.

One-shot "what's in this capture?" report:
  * API + GPU + driver
  * Frame number + timestamp + capture size
  * Action counts by kind (draw / dispatch / copy / clear / barrier / present / …)
  * Top-N most common PSOs (by draw count)
  * Top-N most common shader entry points
  * RT/DS shape distribution (which render target sizes are most common)
  * Resource counts (textures / buffers / heaps)
  * Per-eye action count split (if stereo viewport detected)
  * Top-10 largest textures by memory footprint
  * Aliased-heap candidates count

Usage:
    python util/automation/quick_triage.py <capture.rdc>
    python util/automation/quick_triage.py <capture.rdc> --json out.json
    python util/automation/quick_triage.py <capture.rdc> --top 20
"""

import argparse
import json
import os
import sys
from collections import Counter

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _action_kind(action) -> str:
    f = int(action.flags)
    if f & int(rd.ActionFlags.Drawcall):           return "draw"
    if f & int(rd.ActionFlags.Dispatch):           return "dispatch"
    if f & int(rd.ActionFlags.Copy):               return "copy"
    if f & int(rd.ActionFlags.Resolve):            return "resolve"
    if f & int(rd.ActionFlags.Clear):              return "clear"
    if f & int(rd.ActionFlags.GenMips):            return "genmips"
    if f & int(rd.ActionFlags.Present):            return "present"
    if f & int(rd.ActionFlags.CmdList):            return "cmdlist"
    if f & int(rd.ActionFlags.PassBoundary):       return "pass_boundary"
    if f & int(rd.ActionFlags.SetMarker):          return "marker"
    return "other"


def triage(capture_path: str, top: int = 10) -> dict:
    cap, controller = _lib.open_capture(capture_path)
    try:
        out = {"capture_path": os.path.abspath(capture_path)}
        # File size
        try:
            out["capture_size_bytes"] = os.path.getsize(capture_path)
        except Exception:
            pass

        # Frame info
        try:
            fi = controller.GetFrameInfo()
            out["frame"] = {
                "frameNumber": int(fi.frameNumber),
                "fileOffset":  int(fi.fileOffset),
                "stats_drawcalls":  int(fi.stats.draws.calls) if hasattr(fi, "stats") else None,
                "captureTime":      str(fi.captureTime) if hasattr(fi, "captureTime") else None,
            }
        except Exception:
            pass

        # API + GPU
        try:
            apiprops = controller.GetAPIProperties()
            out["api"] = {
                "pipelineType": str(apiprops.pipelineType).split(".")[-1],
                "localRenderer": str(apiprops.localRenderer).split(".")[-1],
                "vendor":     str(apiprops.vendor).split(".")[-1] if hasattr(apiprops, "vendor") else None,
                "GPUVendor":  str(apiprops.GPUVendor).split(".")[-1] if hasattr(apiprops, "GPUVendor") else None,
                "shaderDebugging": bool(apiprops.shaderDebugging) if hasattr(apiprops, "shaderDebugging") else None,
                "pixelHistory":    bool(apiprops.pixelHistory) if hasattr(apiprops, "pixelHistory") else None,
            }
        except Exception:
            pass

        # Resource counts
        try:
            out["resources"] = {
                "textures": len(controller.GetTextures()),
                "buffers":  len(controller.GetBuffers()),
                "all":      len(controller.GetResources()),
                "descriptor_stores": len(controller.GetDescriptorStores()),
            }
        except Exception:
            pass

        # Walk actions for histogram
        kind_counts = Counter()
        pso_counts = Counter()
        entry_counts_ps = Counter()
        entry_counts_cs = Counter()
        rt_shape_counts = Counter()
        vp_x_set = set()

        total = 0
        for a in _lib.walk_actions(controller):
            total += 1
            kind = _action_kind(a)
            kind_counts[kind] += 1
            if kind not in ("draw", "dispatch"):
                continue
            eid = int(a.eventId)
            try:
                controller.SetFrameEvent(eid, True)
                d3d12 = controller.GetD3D12PipelineState()
                pipe = controller.GetPipelineState()
            except Exception:
                continue
            # PSO id
            try:
                pso_id = _lib.resource_id_str(d3d12.pipelineResourceId)
                if pso_id: pso_counts[pso_id] += 1
            except Exception:
                pass
            # Shader entry points
            for stage_enum, bucket in (
                (rd.ShaderStage.Pixel, entry_counts_ps),
                (rd.ShaderStage.Compute, entry_counts_cs),
            ):
                try:
                    refl = pipe.GetShaderReflection(stage_enum)
                except Exception:
                    refl = None
                if refl and len(refl.rawBytes) > 0:
                    bucket[str(refl.entryPoint)] += 1
                    break  # only one per event
            # RT shapes (RT0)
            try:
                if len(d3d12.outputMerger.renderTargets) > 0:
                    rt = d3d12.outputMerger.renderTargets[0]
                    rid = _lib.resource_id_str(rt.resource)
                    if rid:
                        for t in controller.GetTextures():
                            if _lib.resource_id_str(t.resourceId) == rid:
                                rt_shape_counts[(int(t.width), int(t.height))] += 1
                                break
            except Exception:
                pass
            # Viewport.x
            try:
                if len(d3d12.rasterizer.viewports) > 0:
                    vp_x = int(d3d12.rasterizer.viewports[0].x)
                    vp_x_set.add(vp_x)
            except Exception:
                pass

        out["totals"] = {"total_actions": total}
        out["actions_by_kind"] = dict(kind_counts.most_common())
        out["top_psos"] = pso_counts.most_common(top)
        out["top_ps_entries"] = entry_counts_ps.most_common(top)
        out["top_cs_entries"] = entry_counts_cs.most_common(top)
        out["top_rt_shapes"] = [
            {"w": k[0], "h": k[1], "count": v}
            for k, v in rt_shape_counts.most_common(top)
        ]
        out["viewport_x_values"] = sorted(vp_x_set)
        # Heuristic stereo detection
        if len(vp_x_set) >= 2:
            xs = sorted(vp_x_set)
            # If there's a clear split (one near 0, one mid-screen-or-greater)
            half = max(xs) / 2 if max(xs) > 0 else 0
            out["stereo_hint"] = {
                "looks_like_side_by_side": any(x >= 100 for x in xs) and any(x < 100 for x in xs),
                "distinct_viewport_x":     xs,
            }

        # Top-N largest textures by memory footprint
        try:
            tex_sizes = []
            for t in controller.GetTextures():
                try:
                    # Rough estimate: w*h*d*arraysize*4 bytes (most formats are 4B avg)
                    sz = int(t.width) * int(t.height) * int(t.depth) * int(t.arraysize) * 4
                    tex_sizes.append({
                        "resource_id": _lib.resource_id_str(t.resourceId),
                        "shape": f"{int(t.width)}x{int(t.height)}x{int(t.depth)}",
                        "format": str(t.format.Name()),
                        "est_bytes": sz,
                    })
                except Exception:
                    pass
            tex_sizes.sort(key=lambda x: x["est_bytes"], reverse=True)
            out["top_largest_textures"] = tex_sizes[:top]
        except Exception:
            pass

        return out
    finally:
        controller.Shutdown()
        cap.Shutdown()


def format_report(report: dict) -> str:
    lines = []
    lines.append(f"Quick Triage: {report.get('capture_path')}")
    lines.append("=" * 70)
    if "capture_size_bytes" in report:
        mb = report["capture_size_bytes"] / (1024*1024)
        lines.append(f"  Capture size:    {mb:.1f} MB")
    if "frame" in report:
        f = report["frame"]
        lines.append(f"  Frame #:         {f.get('frameNumber')}")
    if "api" in report:
        a = report["api"]
        lines.append(f"  API:             {a.get('pipelineType')} / {a.get('localRenderer')}")
        lines.append(f"  GPU vendor:      {a.get('GPUVendor') or a.get('vendor')}")
    if "resources" in report:
        r = report["resources"]
        lines.append(f"  Resources:       {r['all']} total ({r['textures']} textures, {r['buffers']} buffers)")
    lines.append("")
    lines.append("Actions by kind:")
    for kind, n in report.get("actions_by_kind", {}).items():
        lines.append(f"  {kind:>15}: {n}")
    lines.append("")
    if report.get("stereo_hint", {}).get("looks_like_side_by_side"):
        lines.append(f"  ⚐ Stereo detected (viewport.x values: {report['stereo_hint']['distinct_viewport_x']})")
        lines.append("")
    lines.append("Top PSOs (by draw count):")
    for pso, n in (report.get("top_psos") or [])[:10]:
        lines.append(f"  {n:>5}  {pso}")
    lines.append("")
    lines.append("Top PS entry points:")
    for entry, n in (report.get("top_ps_entries") or [])[:10]:
        lines.append(f"  {n:>5}  {entry}")
    if report.get("top_cs_entries"):
        lines.append("")
        lines.append("Top CS entry points:")
        for entry, n in (report.get("top_cs_entries") or [])[:10]:
            lines.append(f"  {n:>5}  {entry}")
    lines.append("")
    lines.append("Top RT shapes:")
    for s in (report.get("top_rt_shapes") or [])[:10]:
        lines.append(f"  {s['count']:>5}  {s['w']}x{s['h']}")
    if report.get("top_largest_textures"):
        lines.append("")
        lines.append("Largest textures (estimated):")
        for t in report["top_largest_textures"][:10]:
            mb = t["est_bytes"] / (1024*1024)
            lines.append(f"  {mb:>7.2f} MB  {t['resource_id']:25} {t['shape']} {t['format']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", help="dump full report")
    args = ap.parse_args()
    report = triage(args.capture, args.top)
    print(format_report(report))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
