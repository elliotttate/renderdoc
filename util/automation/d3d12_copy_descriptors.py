"""Driver-recorded CopyDescriptors / CreateXxxView event log.

RenderDoc already serialises every D3D12 descriptor mutation as a
structured chunk (D3D12Chunk::Device_CopyDescriptors,
Device_CopyDescriptorsSimple, Device_CreateConstantBufferView,
Device_CreateShaderResourceView, Device_CreateUnorderedAccessView,
Device_CreateRenderTargetView, Device_CreateDepthStencilView,
Device_CreateSampler). This module walks controller.GetStructuredFile()
and produces a chronological log of every descriptor write — the *real*
driver-recorded answer to "what mutated this heap slot and when".

Combined with descriptor_history.py (which sees the resolved per-event
state), this gives us full coverage: every write event by chunk and every
observable change at consumption events. They should reconcile.

Output: JSONL of {chunkIndex, chunkName, threadID, timestamp, args}

Usage:
    python -m util.automation.d3d12_copy_descriptors <cap.rdc> [--out file]
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


DESCRIPTOR_WRITE_CHUNK_NAMES = {
    "ID3D12Device::CopyDescriptors",
    "ID3D12Device::CopyDescriptorsSimple",
    "ID3D12Device::CreateConstantBufferView",
    "ID3D12Device::CreateShaderResourceView",
    "ID3D12Device::CreateUnorderedAccessView",
    "ID3D12Device::CreateRenderTargetView",
    "ID3D12Device::CreateDepthStencilView",
    "ID3D12Device::CreateSampler",
}


def _obj_to_jsonable(obj, depth=0, max_depth=4):
    """Convert an SDObject to a JSON-safe dict/list."""
    if obj is None or depth >= max_depth:
        return None
    try:
        n_children = obj.NumChildren() if hasattr(obj, "NumChildren") else 0
    except Exception:
        n_children = 0
    if n_children == 0:
        # Leaf — read scalar value
        try:
            return obj.data.basic.u
        except Exception:
            try:
                return float(obj.data.basic.d)
            except Exception:
                try:
                    return str(obj.AsString())
                except Exception:
                    return None
    out = {}
    for i in range(min(n_children, 32)):
        c = obj.GetChild(i)
        out[str(c.name)] = _obj_to_jsonable(c, depth + 1, max_depth)
    return out


def extract(capture: str) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        sdfile = controller.GetStructuredFile()
        rows = []
        for ci, chunk in enumerate(sdfile.chunks):
            name = str(chunk.name)
            if name not in DESCRIPTOR_WRITE_CHUNK_NAMES:
                continue
            args = {}
            try:
                n_children = chunk.NumChildren()
            except Exception:
                n_children = 0
            for i in range(min(n_children, 16)):
                c = chunk.GetChild(i)
                args[str(c.name)] = _obj_to_jsonable(c)
            rows.append({
                "chunkIndex": ci,
                "name": name,
                "threadID": int(chunk.metadata.threadID) if hasattr(chunk.metadata, "threadID") else None,
                "timestampMicro": int(chunk.metadata.timestampMicro) if hasattr(chunk.metadata, "timestampMicro") else None,
                "args": args,
            })

        # Stats per chunk type
        by_kind = {}
        for r in rows:
            by_kind[r["name"]] = by_kind.get(r["name"], 0) + 1

        return {"summary": by_kind, "total": len(rows), "events": rows}
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Dump every driver-recorded descriptor write.")
    p.add_argument("capture")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = extract(args.capture)
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
