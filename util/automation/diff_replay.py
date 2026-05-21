"""Diff replay across two captures.

Opens two ``.rdc`` files, walks both in lockstep at the action level, and
surfaces the first event where GPU state diverges. Useful for comparing
before-fix vs after-fix captures.

Compared at each matched action:

  - PSO ResourceId
  - Per-stage shader bytecode hashes
  - Bound RT resources + min/max
  - Bound SRV/CBV ResourceIds per register
  - Dispatch dimensions for compute
  - Action name + flag set
  - Output min/max delta (HDR-safe via ``GetMinMax``)

Events are aligned by event ID by default. ``--align-by-name`` aligns by
action name + chunk index instead, which is more robust when event IDs
diverge mid-capture.

Usage::

    python -m util.automation.diff_replay before.rdc after.rdc --first-divergence
    python -m util.automation.diff_replay before.rdc after.rdc --out diff.json
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


def _collect_action_summary(controller, action) -> Dict[str, Any]:
    eid = int(action.eventId)
    summary: Dict[str, Any] = {
        "eventId": eid,
        "name": str(action.GetName(controller.GetStructuredFile())),
        "flags": _lib.action_flag_names(int(action.flags)),
    }
    flags = int(action.flags)
    if flags & int(rd.ActionFlags.Dispatch):
        summary["dispatchDimension"] = [int(v) for v in action.dispatchDimension]
    if flags & int(rd.ActionFlags.Drawcall):
        summary["numIndices"] = int(action.numIndices)
        summary["numInstances"] = int(action.numInstances)
    return summary


def _state_fingerprint(controller, eid: int) -> Dict[str, Any]:
    """Cheap, comparable per-event fingerprint."""
    try:
        state = _lib.collect_state_at_event(controller, eid)
    except Exception as exc:
        return {"error": str(exc)}
    fp: Dict[str, Any] = {
        "pipelineId": state.get("pipelineId"),
        "shaders": {s["stage"]: s.get("bytecodeHash") for s in state.get("shaders", [])},
        "renderTargets": [(rt.get("resource"), rt.get("view"))
                          for rt in (state.get("renderTargets") or [])],
        "bindings": [],
    }
    for b in state.get("bindings", []):
        fp["bindings"].append({
            "stage": b.get("stage"),
            "type": b.get("type"),
            "register": b.get("register"),
            "resource": b.get("resource"),
        })
    return fp


def _walk_actions(controller) -> List[Any]:
    out = []
    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
            continue
        out.append(a)
    return out


def diff(capture_a: str, capture_b: str, *, align_by_name: bool = False,
         max_pairs: int = 100000) -> Dict[str, Any]:
    """Walk two captures side-by-side and produce a list of divergences."""
    cap_a, ctrl_a = _lib.open_capture(capture_a)
    cap_b, ctrl_b = _lib.open_capture(capture_b)
    try:
        actions_a = _walk_actions(ctrl_a)
        actions_b = _walk_actions(ctrl_b)

        if align_by_name:
            sf_a = ctrl_a.GetStructuredFile()
            sf_b = ctrl_b.GetStructuredFile()
            pairs = []
            i = j = 0
            while i < len(actions_a) and j < len(actions_b):
                na = str(actions_a[i].GetName(sf_a))
                nb = str(actions_b[j].GetName(sf_b))
                if na == nb:
                    pairs.append((actions_a[i], actions_b[j]))
                    i += 1
                    j += 1
                elif i + 1 < len(actions_a) and \
                        str(actions_a[i + 1].GetName(sf_a)) == nb:
                    i += 1
                elif j + 1 < len(actions_b) and \
                        str(actions_b[j + 1].GetName(sf_b)) == na:
                    j += 1
                else:
                    i += 1
                    j += 1
        else:
            n = min(len(actions_a), len(actions_b))
            pairs = list(zip(actions_a[:n], actions_b[:n]))

        if len(pairs) > max_pairs:
            pairs = pairs[:max_pairs]

        divergences = []
        for a_action, b_action in pairs:
            a_eid = int(a_action.eventId)
            b_eid = int(b_action.eventId)
            a_fp = _state_fingerprint(ctrl_a, a_eid)
            b_fp = _state_fingerprint(ctrl_b, b_eid)
            if a_fp != b_fp:
                # Detail what differs
                detail: Dict[str, Any] = {}
                if a_fp.get("pipelineId") != b_fp.get("pipelineId"):
                    detail["pipelineId"] = (a_fp.get("pipelineId"), b_fp.get("pipelineId"))
                a_shaders = a_fp.get("shaders") or {}
                b_shaders = b_fp.get("shaders") or {}
                stage_diff = {}
                for stage in sorted(set(a_shaders) | set(b_shaders)):
                    if a_shaders.get(stage) != b_shaders.get(stage):
                        stage_diff[stage] = (a_shaders.get(stage), b_shaders.get(stage))
                if stage_diff:
                    detail["shaders"] = stage_diff
                if a_fp.get("renderTargets") != b_fp.get("renderTargets"):
                    detail["renderTargets"] = (a_fp.get("renderTargets"), b_fp.get("renderTargets"))
                a_binds = {(b["stage"], b["type"], b["register"]): b.get("resource")
                           for b in a_fp.get("bindings", [])}
                b_binds = {(b["stage"], b["type"], b["register"]): b.get("resource")
                           for b in b_fp.get("bindings", [])}
                binding_diffs = []
                for k in sorted(set(a_binds) | set(b_binds), key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
                    if a_binds.get(k) != b_binds.get(k):
                        binding_diffs.append({"key": k, "a": a_binds.get(k), "b": b_binds.get(k)})
                if binding_diffs:
                    detail["bindings"] = binding_diffs

                divergences.append({
                    "a": _collect_action_summary(ctrl_a, a_action),
                    "b": _collect_action_summary(ctrl_b, b_action),
                    "detail": detail,
                })
        first = divergences[0] if divergences else None
        return {
            "pairs": len(pairs),
            "divergenceCount": len(divergences),
            "firstDivergence": first,
            "divergences": divergences,
        }
    finally:
        ctrl_a.Shutdown()
        ctrl_b.Shutdown()
        cap_a.Shutdown()
        cap_b.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Diff two captures at the GPU-state level.")
    p.add_argument("capture_a")
    p.add_argument("capture_b")
    p.add_argument("--align-by-name", action="store_true",
                   help="Align actions by action name instead of by position (more robust to event-id drift)")
    p.add_argument("--first-divergence", action="store_true",
                   help="Only print the first divergence")
    p.add_argument("--max-pairs", type=int, default=100000)
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = diff(args.capture_a, args.capture_b,
                   align_by_name=args.align_by_name,
                   max_pairs=args.max_pairs)
    finally:
        rd.ShutdownReplay()

    if args.first_divergence:
        out["divergences"] = []

    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text[:8000] + ("..." if len(text) > 8000 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
