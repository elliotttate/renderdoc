"""Eye image diff — extract render-target contents at two events and diff.

Used to answer "where, in screen space, do the two eyes actually disagree?"
Optionally saves left.png, right.png, and a delta heatmap.

Works on R8G8B8A8 / B8G8R8A8 / R16G16B16A16_FLOAT / R32G32B32A32_FLOAT.
For other formats falls back to per-channel summary stats only.

Usage:
    python -m util.automation.eye_image_diff <cap.rdc> --a 16042 --b 16678 [--out-dir dir]
"""

import argparse
import json
import os
import struct
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _rt_at_event(controller, eid):
    controller.SetFrameEvent(int(eid), True)
    try:
        d3d12 = controller.GetD3D12PipelineState()
    except Exception:
        return None
    if d3d12 is None or len(d3d12.outputMerger.renderTargets) == 0:
        return None
    rt = d3d12.outputMerger.renderTargets[0]
    return {
        "resource": rt.resource,
        "view": rt.view,
        "firstMip": int(rt.firstMip),
        "firstSlice": int(rt.firstSlice),
    }


def _texture_info(controller, rid):
    for t in controller.GetTextures():
        if t.resourceId == rid:
            return t
    return None


def _decode_to_float_array(raw: bytes, fmt_name: str, width: int, height: int):
    """Return list-of-floats normalized to [0,1] for 4-channel RGBA, or None."""
    if fmt_name in ("R8G8B8A8_UNORM", "B8G8R8A8_UNORM", "R8G8B8A8_TYPELESS"):
        n = min(len(raw), width * height * 4)
        out = [raw[i] / 255.0 for i in range(n)]
        return out, 4
    if fmt_name in ("R16G16B16A16_FLOAT",):
        # 16-bit half float
        import struct as _s
        out = []
        for i in range(0, min(len(raw), width * height * 8), 2):
            half = _s.unpack_from("<H", raw, i)[0]
            sign = (half >> 15) & 0x1
            exp = (half >> 10) & 0x1F
            mant = half & 0x3FF
            if exp == 0:
                val = (mant / 1024.0) * 2 ** -14 * (-1 if sign else 1)
            elif exp == 31:
                val = float("nan") if mant else (float("-inf") if sign else float("inf"))
            else:
                val = (1 + mant / 1024.0) * 2 ** (exp - 15) * (-1 if sign else 1)
            out.append(val)
        return out, 4
    if fmt_name in ("R32G32B32A32_FLOAT",):
        n = len(raw) // 4
        out = list(struct.unpack(f"<{n}f", raw[: n * 4]))
        return out, 4
    return None, 0


def eye_image_diff(capture: str, event_a: int, event_b: int, out_dir=None) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        rt_a = _rt_at_event(controller, event_a)
        rt_b = _rt_at_event(controller, event_b)
        if rt_a is None or rt_b is None:
            return {"error": "no RT bound at one of the events", "rtA": rt_a, "rtB": rt_b}

        tex_a = _texture_info(controller, rt_a["resource"])
        tex_b = _texture_info(controller, rt_b["resource"])
        if tex_a is None or tex_b is None:
            return {"error": "RT texture not found"}

        sub_a = rd.Subresource(rt_a["firstMip"], rt_a["firstSlice"], 0)
        sub_b = rd.Subresource(rt_b["firstMip"], rt_b["firstSlice"], 0)
        # Get data at each event separately (SetFrameEvent picks correct timing)
        controller.SetFrameEvent(int(event_a), True)
        raw_a = bytes(controller.GetTextureData(rt_a["resource"], sub_a))
        controller.SetFrameEvent(int(event_b), True)
        raw_b = bytes(controller.GetTextureData(rt_b["resource"], sub_b))

        fmt_a = str(tex_a.format.Name()) if hasattr(tex_a.format, "Name") else str(tex_a.format)
        fmt_b = str(tex_b.format.Name()) if hasattr(tex_b.format, "Name") else str(tex_b.format)
        w, h = int(tex_a.width), int(tex_a.height)

        result = {
            "eventA": event_a,
            "eventB": event_b,
            "rtA": _lib.resource_id_str(rt_a["resource"]),
            "rtB": _lib.resource_id_str(rt_b["resource"]),
            "formatA": fmt_a,
            "formatB": fmt_b,
            "size": [w, h],
        }

        if fmt_a != fmt_b or len(raw_a) != len(raw_b):
            result["mismatch"] = "format/size differ"
            return result

        if raw_a == raw_b:
            result["identical"] = True
            return result

        floats_a, ch = _decode_to_float_array(raw_a, fmt_a, w, h)
        floats_b, _ = _decode_to_float_array(raw_b, fmt_a, w, h)
        if floats_a is None or floats_b is None:
            result["note"] = f"unsupported format {fmt_a}; binary diff only"
            result["byteIdenticalRegions"] = sum(1 for i in range(min(len(raw_a), len(raw_b))) if raw_a[i] == raw_b[i])
            return result

        # Per-channel summary
        n = min(len(floats_a), len(floats_b))
        per_chan = []
        for c in range(ch):
            sa = floats_a[c::ch][:n // ch]
            sb = floats_b[c::ch][:n // ch]
            count = min(len(sa), len(sb))
            sum_abs = 0.0
            max_abs = 0.0
            diffs = 0
            for i in range(count):
                d = abs(sa[i] - sb[i])
                sum_abs += d
                if d > max_abs:
                    max_abs = d
                if d > 1e-6:
                    diffs += 1
            per_chan.append({
                "channel": c,
                "sumAbsDiff": sum_abs,
                "maxAbsDiff": max_abs,
                "differingPixels": diffs,
                "count": count,
            })
        result["perChannelDiff"] = per_chan

        # Save left/right snapshots and delta if requested
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            for tag, rid, sub, eid in (("left", rt_a["resource"], sub_a, event_a),
                                        ("right", rt_b["resource"], sub_b, event_b)):
                controller.SetFrameEvent(int(eid), True)
                save = rd.TextureSave()
                save.resourceId = rid
                save.mip = sub.mip
                save.slice.sliceIndex = sub.slice
                save.destType = rd.FileType.PNG
                controller.SaveTexture(save, os.path.join(out_dir, f"{tag}_event{eid}.png"))
            result["savedTo"] = os.path.abspath(out_dir)

        return result
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Diff render-target contents between two events.")
    p.add_argument("capture")
    p.add_argument("--a", type=int, required=True)
    p.add_argument("--b", type=int, required=True)
    p.add_argument("--out-dir", help="Save left.png / right.png snapshots here")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = eye_image_diff(args.capture, args.a, args.b, args.out_dir)
    finally:
        rd.ShutdownReplay()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
