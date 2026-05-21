"""Eye-bug fix proposal generator.

Given a divergent (left, right) event pair (typically the top-scored
pair from ``stereo_divergence.py``), emit a starter UEVR-compatible
override manifest that nudges the right eye's bindings toward the left's.

Three proposal kinds, picked by what differs between the eyes:

  - **Divergent SRV**: emit a ``redirect_handle`` DXIL transform that
    tells the right eye's shader to read from the same slot as the
    left eye does.
  - **Divergent CBV bytes**: emit a ``replace_cbuffer_extract_literal``
    transform for each bytewise-differing field, or a ``bind_override``
    that ships left's bytes for the right's CBV.
  - **Divergent shader hash**: surface the bytecode pair so the user
    can choose to swap the right shader for the left one.

The output is intentionally written with ``enabled: false`` so it lands
in the shader_overrides directory without auto-applying. The user flips
it on after review.

Usage::

    python -m util.automation.fix_proposal <cap.rdc> --left N --right M --out proposal.json
"""

import argparse
import json
import os
import struct
import sys
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _bindings_by_key(state) -> Dict[Any, Dict[str, Any]]:
    out = {}
    for b in state.get("bindings", []):
        out[(b.get("stage"), b.get("type"), b.get("register"))] = b
    return out


def _read_cb(controller, state, stage_name: str) -> Optional[bytes]:
    """Read the first constant buffer bound to ``stage_name``."""
    stage = getattr(rd.ShaderStage, stage_name, None)
    if stage is None:
        return None
    try:
        arr = controller.GetPipelineState().GetConstantBlocks(stage, False)
    except Exception:
        return None
    if not arr or len(arr) == 0:
        return None
    used = arr[0]
    desc = used.descriptor
    if desc is None or desc.resource is None:
        return None
    try:
        return bytes(controller.GetBufferData(
            desc.resource, int(desc.byteOffset),
            min(int(desc.byteSize) or 256, 4096)))
    except Exception:
        return None


def _diff_cb_floats(left_bytes: bytes, right_bytes: bytes,
                    max_diffs: int = 32) -> List[Dict[str, Any]]:
    """Return the first N float-index differences between two cbuffer dumps."""
    n = min(len(left_bytes), len(right_bytes)) // 4
    out = []
    for i in range(n):
        try:
            lf = struct.unpack("<f", left_bytes[i * 4:i * 4 + 4])[0]
            rf = struct.unpack("<f", right_bytes[i * 4:i * 4 + 4])[0]
        except Exception:
            continue
        # Filter out tiny float drift; floats with identical bytes are excluded
        if left_bytes[i * 4:i * 4 + 4] != right_bytes[i * 4:i * 4 + 4]:
            out.append({
                "floatIndex": i, "byteOffset": i * 4,
                "left": lf, "right": rf,
            })
            if len(out) >= max_diffs:
                break
    return out


def propose(capture_path: str, left_eid: int, right_eid: int) -> Dict[str, Any]:
    cap, controller = _lib.open_capture(capture_path)
    try:
        left_state = _lib.collect_state_at_event(controller, left_eid)
        right_state = _lib.collect_state_at_event(controller, right_eid)

        proposals: List[Dict[str, Any]] = []

        # 1. Divergent PSO / shader hash → shader swap suggestion
        sh_l = {s["stage"]: s for s in left_state.get("shaders", [])}
        sh_r = {s["stage"]: s for s in right_state.get("shaders", [])}
        for stage in sorted(set(sh_l.keys()) | set(sh_r.keys())):
            l = sh_l.get(stage) or {}
            r = sh_r.get(stage) or {}
            if l.get("bytecodeHash") != r.get("bytecodeHash"):
                proposals.append({
                    "kind": "shader_swap",
                    "stage": stage,
                    "leftHash": l.get("bytecodeHash"),
                    "rightHash": r.get("bytecodeHash"),
                    "suggestion": (
                        f"Right eye uses a different {stage} shader hash. "
                        f"Investigate why — possibly different material permutation. "
                        f"To override: substitute right's {stage} shader bytecode with "
                        f"shaders/{l.get('bytecodeHash')}.cso."
                    ),
                    "manifest": {
                        "enabled": False,
                        "targetHash": r.get("bytecodeHash"),
                        "stage": stage.lower(),
                        "rightPayload": {
                            "kind": "Bytecode",
                            "sourceFile": f"shaders/{l.get('bytecodeHash')}.cso",
                        },
                    },
                })

        # 2. Divergent SRV bindings → redirect_handle transforms
        bind_l = _bindings_by_key(left_state)
        bind_r = _bindings_by_key(right_state)
        for key in sorted(set(bind_l.keys()) | set(bind_r.keys()),
                          key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
            bl = bind_l.get(key) or {}
            br = bind_r.get(key) or {}
            if bl.get("resource") == br.get("resource"):
                continue
            stage, btype, reg = key
            if btype and btype.startswith("Read"):
                # SRV/UAV divergence — redirect_handle
                proposals.append({
                    "kind": "redirect_handle",
                    "stage": stage,
                    "register": reg,
                    "leftResource": bl.get("resource"),
                    "rightResource": br.get("resource"),
                    "suggestion": (
                        f"Right eye samples {br.get('resource')} at {stage} register {reg}; "
                        f"left samples {bl.get('resource')}. "
                        f"Force right to sample from left's resource at this register."
                    ),
                    "manifest": {
                        "enabled": False,
                        "transforms": [
                            {
                                "kind": "redirect_handle",
                                "stage": stage.lower(),
                                "resource_class": "srv" if "ReadOnly" in (btype or "") else "uav",
                                "register": reg,
                                "from_resource": br.get("resource"),
                                "to_resource": bl.get("resource"),
                                "required": True,
                            }
                        ],
                    },
                })
            elif btype and btype.startswith("ConstantBlock"):
                # CBV resource divergence — bind_override pointing at left's CBV
                proposals.append({
                    "kind": "cbv_redirect",
                    "stage": stage,
                    "register": reg,
                    "leftResource": bl.get("resource"),
                    "rightResource": br.get("resource"),
                    "suggestion": (
                        f"Right eye binds a different CBV at {stage} cb{reg}. "
                        f"Override the right-eye binding to use left's CBV."
                    ),
                    "manifest": {
                        "enabled": False,
                        "bindOverride": {
                            "stage": stage.lower(),
                            "register": reg,
                            "fromResource": br.get("resource"),
                            "toResource": bl.get("resource"),
                        },
                    },
                })

        # 3. CBV bytewise divergence — replace_cbuffer_extract_literal candidates
        for stage_name in ("Pixel", "Vertex", "Compute"):
            l_bytes = _read_cb(controller, None, stage_name)  # noop helper signature
            # Actually set the event first and read
            controller.SetFrameEvent(left_eid, True)
            l_bytes = _read_cb(controller, left_state, stage_name)
            controller.SetFrameEvent(right_eid, True)
            r_bytes = _read_cb(controller, right_state, stage_name)
            if l_bytes is None or r_bytes is None:
                continue
            if l_bytes == r_bytes:
                continue
            diffs = _diff_cb_floats(l_bytes, r_bytes)
            if not diffs:
                continue
            proposals.append({
                "kind": "cb_byte_override",
                "stage": stage_name,
                "leftBytesSha": _sha_short(l_bytes),
                "rightBytesSha": _sha_short(r_bytes),
                "topDiffs": diffs[:8],
                "suggestion": (
                    f"{stage_name} cb0 bytes differ at {len(diffs)} float positions. "
                    f"Top divergent float indices: {[d['floatIndex'] for d in diffs[:4]]}. "
                    f"If one of these is a per-view offset (e.g. View[148].x for SBS), "
                    f"override that 4-byte slot on right eye to match left's value."
                ),
                "manifest": {
                    "enabled": False,
                    "transforms": [
                        {
                            "kind": "replace_cbuffer_extract_literal",
                            "stage": stage_name.lower(),
                            "cb_index": 0,
                            "float_index": d["floatIndex"],
                            "value": d["left"],
                            "required": True,
                        }
                        for d in diffs[:8]
                    ],
                },
            })

        return {
            "leftEventId": left_eid,
            "rightEventId": right_eid,
            "proposalCount": len(proposals),
            "proposals": proposals,
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def _sha_short(b: bytes) -> str:
    import hashlib
    return hashlib.md5(b).hexdigest()[:16]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Emit starter UEVR override manifests for a divergent L/R pair.")
    p.add_argument("capture")
    p.add_argument("--left", type=int, required=True)
    p.add_argument("--right", type=int, required=True)
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = propose(args.capture, args.left, args.right)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
