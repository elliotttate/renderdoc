"""Resource resolver — find any RenderDoc resource by name / handle /
partial ID / dimensions+format / format-shape.

Bucketing: for each match, list every event that mentions it bucketed by
role (create / write / read / copy_src / copy_dst / barrier / destroy / other).

This generalizes resource_lineage.py with much more flexible match modes
and a structured per-role role report. Nsight's `ngfx_resolve_handle`
equivalent.

Usage from Python:

    from util.automation import resource_resolver
    hits = resource_resolver.resolve(
        capture_path="path.rdc",
        match=resource_resolver.Match(
            name_regex=r"^TranslucentLight.*",
            # or: resource_id="ResourceId::141231",
            # or: dim=(24, 24, 24), format_substr="R16G16B16A16",
        ),
    )
    for h in hits:
        print(h.resource_id, h.summary, len(h.events_by_role["read"]))

CLI:
    python util/automation/resource_resolver.py capture.rdc --name "Translucent.*"
    python util/automation/resource_resolver.py capture.rdc --id ResourceId::141231
    python util/automation/resource_resolver.py capture.rdc --dim 24,24,24 --format R16G16B16A16_FLOAT
    python util/automation/resource_resolver.py capture.rdc --any 141231
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


@dataclass
class Match:
    resource_id: Optional[str] = None
    resource_id_substr: Optional[str] = None
    name_regex: Optional[str] = None
    dim: Optional[Tuple[int, int, int]] = None      # (w, h, d)
    format_substr: Optional[str] = None             # e.g. "R16G16B16A16"
    is_3d: Optional[bool] = None
    any_str: Optional[str] = None                   # match name OR id-substr


@dataclass
class ResolvedHit:
    resource_id: str
    name: Optional[str]
    summary: str
    info: dict
    events_by_role: dict = field(default_factory=dict)


def _classify_usage(kind_str: str) -> str:
    if "Discard" in kind_str:           return "discard"
    if "Barrier" in kind_str:           return "barrier"
    if "CopyDst" in kind_str:           return "copy_dst"
    if "CopySrc" in kind_str:           return "copy_src"
    if "ResolveDst" in kind_str or "Resolve" in kind_str: return "resolve"
    if "RWResource" in kind_str:        return "write_uav"
    if "ColorTarget" in kind_str or "ColourTarget" in kind_str: return "write_rt"
    if "DepthStencilTarget" in kind_str: return "write_ds"
    if "Clear" in kind_str:             return "clear"
    if "GenMips" in kind_str:           return "genmips"
    if "Resource" in kind_str:          return "read"
    return "other"


def _texture_info(t) -> dict:
    return {
        "kind": "Texture",
        "type": str(t.type).split(".")[-1] if hasattr(t, "type") else None,
        "dimension": int(t.dimension) if hasattr(t, "dimension") else None,
        "width":  int(t.width),
        "height": int(t.height),
        "depth":  int(t.depth),
        "arraysize": int(t.arraysize),
        "mips":   int(t.mips),
        "samples": int(t.msSamp) if hasattr(t, "msSamp") else 1,
        "format": str(t.format.Name()),
        "creationFlags": str(t.creationFlags).split(".")[-1] if hasattr(t, "creationFlags") else None,
    }


def _buffer_info(b) -> dict:
    return {
        "kind": "Buffer",
        "length": int(b.length),
        "creationFlags": str(b.creationFlags).split(".")[-1] if hasattr(b, "creationFlags") else None,
    }


def _matches(match: Match, rid_str: str, name: Optional[str], info: dict) -> bool:
    if match.resource_id and rid_str != match.resource_id:
        return False
    if match.resource_id_substr and match.resource_id_substr not in rid_str:
        return False
    if match.name_regex:
        if not name or not re.search(match.name_regex, name):
            return False
    if match.dim and info.get("kind") == "Texture":
        d = (info["width"], info["height"], info["depth"])
        if tuple(d) != tuple(match.dim):
            return False
    if match.format_substr:
        fmt = info.get("format", "")
        if match.format_substr.lower() not in fmt.lower():
            return False
    if match.is_3d is not None and info.get("kind") == "Texture":
        is_3d = info["depth"] > 1
        if is_3d != match.is_3d:
            return False
    if match.any_str:
        s = match.any_str.lower()
        hit = s in (rid_str or "").lower()
        if name: hit = hit or s in name.lower()
        if not hit:
            return False
    return True


def resolve(capture_path: str, match: Match) -> list:
    cap, controller = _lib.open_capture(capture_path)
    try:
        textures = {_lib.resource_id_str(t.resourceId): t for t in controller.GetTextures()}
        buffers  = {_lib.resource_id_str(b.resourceId): b for b in controller.GetBuffers()}
        all_resources = controller.GetResources()
        name_by_id = {}
        for r in all_resources:
            rid = _lib.resource_id_str(r.resourceId)
            try:
                nm = str(r.name)
            except Exception:
                nm = None
            if rid:
                name_by_id[rid] = nm

        hits = []
        for rid_str, name in name_by_id.items():
            if rid_str in textures:
                info = _texture_info(textures[rid_str])
            elif rid_str in buffers:
                info = _buffer_info(buffers[rid_str])
            else:
                info = {"kind": "Other"}
            if not _matches(match, rid_str, name, info):
                continue
            # Build the summary
            summary_parts = [info.get("kind", "?")]
            if info.get("kind") == "Texture":
                summary_parts.append(f"{info['width']}x{info['height']}x{info['depth']}")
                summary_parts.append(info["format"])
                if info["depth"] > 1: summary_parts.append("[3D]")
            elif info.get("kind") == "Buffer":
                summary_parts.append(f"{info['length']} bytes")
            if name:
                summary_parts.append(f'"{name}"')
            # Bucket usages
            target = None
            for r in all_resources:
                if _lib.resource_id_str(r.resourceId) == rid_str:
                    target = r.resourceId; break
            events_by_role = {}
            if target is not None:
                usage = controller.GetUsage(target)
                for u in usage:
                    role = _classify_usage(str(u.usage).split(".")[-1])
                    events_by_role.setdefault(role, []).append(int(u.eventId))
            hits.append(ResolvedHit(
                resource_id=rid_str,
                name=name,
                summary=" ".join(summary_parts),
                info=info,
                events_by_role=events_by_role,
            ))
        return hits
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--id", help="exact ResourceId")
    ap.add_argument("--id-substr", help="ResourceId substring match")
    ap.add_argument("--name", help="name regex")
    ap.add_argument("--any", help="match name OR id-substr (case insensitive)")
    ap.add_argument("--dim", help="dimensions WxHxD or W,H,D")
    ap.add_argument("--format", help="format substring (e.g. R16G16B16A16)")
    ap.add_argument("--3d", dest="is_3d", action="store_true")
    ap.add_argument("--2d", dest="is_2d", action="store_true")
    ap.add_argument("--json", help="dump full result")
    args = ap.parse_args()

    dim = None
    if args.dim:
        sep = "," if "," in args.dim else "x"
        try:
            parts = [int(p) for p in args.dim.split(sep)]
            if len(parts) == 3:
                dim = tuple(parts)
        except Exception:
            pass

    is_3d = None
    if args.is_3d: is_3d = True
    elif args.is_2d: is_3d = False

    match = Match(
        resource_id=args.id,
        resource_id_substr=args.id_substr,
        name_regex=args.name,
        dim=dim,
        format_substr=args.format,
        is_3d=is_3d,
        any_str=args.any,
    )

    hits = resolve(args.capture, match)
    print(f"\n{len(hits)} resource match(es):\n")
    for h in hits:
        print(f"  {h.resource_id:25} {h.summary}")
        for role, eids in sorted(h.events_by_role.items()):
            sample = ", ".join(str(e) for e in eids[:5])
            more = f" (+{len(eids)-5} more)" if len(eids) > 5 else ""
            print(f"    {role:9}: {len(eids):4} events  [{sample}{more}]")
        print()

    if args.json:
        out = []
        for h in hits:
            out.append({
                "resource_id": h.resource_id,
                "name": h.name,
                "summary": h.summary,
                "info": h.info,
                "events_by_role": {k: v for k, v in h.events_by_role.items()},
            })
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
