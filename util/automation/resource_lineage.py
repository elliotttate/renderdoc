"""Resource producer/consumer lineage queries.

For a given resource, lists every event that read or wrote it (using
controller.GetUsage()), and optionally walks backwards to find the last writer
before a given event. Mirrors §10 of the roadmap.

Usage:
    python -m util.automation.resource_lineage <capture.rdc> --resource <ResourceId|name> [--before <eid>]
"""

from __future__ import annotations

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


WRITE_USAGES = {
    "VertexBufferOut",
    "StreamOut",
    "DepthStencilTarget",
    "ColorTarget",
    "CPUWrite",
    "GPUWrite",
    "ResolveDst",
    "ResolveSrc",  # context-dependent; included for visibility
    "CopyDst",
    "Clear",
    "GenMips",
    "Discard",
    "Barrier",
    "ReadWriteResource",
}


def _usage_kind(usage) -> str:
    return str(usage).split(".")[-1]


def _find_resource(controller, ref: str):
    """Resolve a resource by either ResourceId string or substring of name."""
    for r in controller.GetResources():
        if str(r.resourceId) == ref or str(r.name).lower() == ref.lower():
            return r.resourceId, str(r.name)
    # substring fallback
    for r in controller.GetResources():
        if ref.lower() in str(r.name).lower():
            return r.resourceId, str(r.name)
    raise ValueError(f"Resource not found: {ref}")


def lineage(capture_path: str, ref: str, before_eid=None) -> dict:
    cap, controller = _lib.open_capture(capture_path)
    try:
        rid, name = _find_resource(controller, ref)
        usages = controller.GetUsage(rid)

        rows = []
        last_writer = None
        for u in usages:
            kind = _usage_kind(u.usage)
            row = {"eventId": int(u.eventId), "usage": kind, "isWrite": kind in WRITE_USAGES}
            rows.append(row)
            if before_eid is not None and int(u.eventId) >= int(before_eid):
                continue
            if row["isWrite"]:
                last_writer = int(u.eventId)

        return {
            "resource": str(rid),
            "name": name,
            "totalUsages": len(rows),
            "lastWriterBefore": last_writer,
            "events": rows,
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Resource producer/consumer lineage.")
    p.add_argument("capture")
    p.add_argument("--resource", "-r", required=True, help="ResourceId string or substring of resource name")
    p.add_argument("--before", type=int, default=None, help="Find the last writer before this eventId")
    p.add_argument("--out", "-o", help="Optional output file (otherwise stdout)")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = lineage(args.capture, args.resource, args.before)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
