"""Resource export and statistics (§14 of the roadmap).

  - export texture (2D/3D/cube) to DDS/PNG/EXR via controller.SaveTexture
  - export buffer to BIN
  - per-channel min/max/mean histogram from GetTextureData
  - "is texture blank?" / "is texture magenta?" probe detectors

Usage:
    python -m util.automation.resource_export <capture.rdc> --resource <ref> --event <eid> [--out <path>] [--stats]
"""

import argparse
import json
import math
import os
import struct
import sys
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _find_resource(controller, ref):
    for r in controller.GetResources():
        if str(r.resourceId) == ref or str(r.name) == ref:
            return r.resourceId, str(r.name)
    for r in controller.GetResources():
        if ref.lower() in str(r.name).lower():
            return r.resourceId, str(r.name)
    raise ValueError(f"Resource not found: {ref}")


def _is_texture(controller, rid):
    for t in controller.GetTextures():
        if t.resourceId == rid:
            return t
    return None


def export(capture: str, ref: str, event_id: Optional[int], out_path: Optional[str], stats: bool) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        rid, name = _find_resource(controller, ref)
        if event_id is not None:
            controller.SetFrameEvent(int(event_id), True)

        tex = _is_texture(controller, rid)
        if tex is not None:
            return _export_texture(controller, tex, out_path, stats)

        # Buffer fallback
        for b in controller.GetBuffers():
            if b.resourceId == rid:
                data = bytes(controller.GetBufferData(rid, 0, int(b.length)))
                if out_path:
                    with open(out_path, "wb") as f:
                        f.write(data)
                return {
                    "resource": str(rid),
                    "name": name,
                    "kind": "buffer",
                    "byteLength": len(data),
                    "outPath": out_path,
                }
        raise ValueError(f"Resource {ref} is neither texture nor buffer")
    finally:
        controller.Shutdown()
        cap.Shutdown()


def _export_texture(controller, tex, out_path, stats):
    rid = tex.resourceId
    info = {
        "resource": str(rid),
        "name": str(tex.name) if hasattr(tex, "name") else None,
        "format": str(tex.format.Name()) if hasattr(tex.format, "Name") else str(tex.format),
        "type": str(tex.type).split(".")[-1],
        "width": int(tex.width),
        "height": int(tex.height),
        "depth": int(tex.depth),
        "mips": int(tex.mips),
        "arraysize": int(tex.arraysize),
    }

    if out_path:
        save = rd.TextureSave()
        save.resourceId = rid
        save.mip = 0
        save.slice.sliceIndex = 0
        ext = os.path.splitext(out_path)[1].lower()
        if ext == ".dds":
            save.destType = rd.FileType.DDS
        elif ext == ".png":
            save.destType = rd.FileType.PNG
        elif ext == ".exr":
            save.destType = rd.FileType.EXR
        elif ext == ".jpg" or ext == ".jpeg":
            save.destType = rd.FileType.JPG
        else:
            save.destType = rd.FileType.DDS
        ok = controller.SaveTexture(save, out_path)
        info["outPath"] = out_path
        info["saved"] = bool(ok)

    if stats:
        info["stats"] = _texture_stats(controller, tex)
    return info


def _texture_stats(controller, tex):
    """Read first slice/mip and compute per-channel min/max/mean + magenta/blank heuristics."""
    rid = tex.resourceId
    sub = rd.Subresource(0, 0, 0)
    raw = bytes(controller.GetTextureData(rid, sub))
    fmt_name = str(tex.format.Name()) if hasattr(tex.format, "Name") else str(tex.format)
    # Only handle a handful of common formats inline; otherwise emit raw size.
    floats = None
    if fmt_name.startswith(("R32G32B32A32_FLOAT", "R16G16B16A16_FLOAT", "R11G11B10_FLOAT",
                            "R32_FLOAT", "D32_FLOAT")):
        bpp = {"R32G32B32A32_FLOAT": 16, "R16G16B16A16_FLOAT": 8, "R11G11B10_FLOAT": 4,
               "R32_FLOAT": 4, "D32_FLOAT": 4}.get(fmt_name.split("_")[0] + "_FLOAT", None)
        # Be lenient — just bytes/4 for float-ish formats:
        if fmt_name == "R32G32B32A32_FLOAT":
            n = len(raw) // 4
            floats = list(struct.unpack(f"<{n}f", raw[: n * 4]))
            channels = 4
        elif fmt_name == "R32_FLOAT" or fmt_name == "D32_FLOAT":
            n = len(raw) // 4
            floats = list(struct.unpack(f"<{n}f", raw[: n * 4]))
            channels = 1
    elif fmt_name.startswith(("R8G8B8A8_UNORM", "B8G8R8A8_UNORM")):
        floats = [b / 255.0 for b in raw]
        channels = 4
    else:
        return {"unsupportedFormat": fmt_name, "byteSize": len(raw)}

    if floats is None or not floats:
        return {"unsupportedFormat": fmt_name, "byteSize": len(raw)}

    per_channel = []
    for c in range(channels):
        vals = floats[c::channels]
        if not vals:
            continue
        mn = min(vals)
        mx = max(vals)
        mean = sum(vals) / len(vals)
        nonzero = sum(1 for v in vals if v != 0.0)
        per_channel.append({"channel": c, "min": mn, "max": mx, "mean": mean, "nonzero": nonzero, "count": len(vals)})

    is_blank = all(c["max"] - c["min"] < 1e-6 and c["mean"] < 1e-6 for c in per_channel)
    is_magenta = False
    if channels >= 3 and len(per_channel) >= 3:
        # mostly (1,0,1)?
        is_magenta = (
            per_channel[0]["mean"] > 0.9
            and per_channel[1]["mean"] < 0.1
            and per_channel[2]["mean"] > 0.9
        )

    return {
        "format": fmt_name,
        "channels": channels,
        "perChannel": per_channel,
        "isBlank": is_blank,
        "isMagentaProbe": is_magenta,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Resource export + statistics.")
    p.add_argument("capture")
    p.add_argument("--resource", "-r", required=True)
    p.add_argument("--event", "-e", type=int, default=None)
    p.add_argument("--out", "-o")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = export(args.capture, args.resource, args.event, args.out, args.stats)
    finally:
        rd.ShutdownReplay()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
