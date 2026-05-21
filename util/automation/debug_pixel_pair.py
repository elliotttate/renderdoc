"""Pair-wise shader trace differ.

Calls ``controller.DebugPixel(x, y)`` on two events (typically a matched
left/right eye pair), walks the resulting ``ShaderDebugTrace`` step-by-step,
and surfaces the first instruction where the live register values
materially diverge.

For SN2: the team has the pso3069 pseudocode and knows the shader is the
same on both eyes. The mystery is *which instruction* in the shader
produces the divergent output. This pinpoints it.

Usage::

    python -m util.automation.debug_pixel_pair <cap.rdc> \
        --left-event LE --right-event RE --x X --y Y [--max-steps 4096]
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


def _snapshot_registers(state) -> Dict[str, Any]:
    """Snapshot all temp/output register values at one ShaderDebugState."""
    out: Dict[str, Any] = {}
    try:
        for sv in state.changes:
            try:
                name = str(sv.before.name) if hasattr(sv, "before") else None
                if not name:
                    continue
                # Just record after value (the new value at this instruction)
                if hasattr(sv, "after"):
                    val = sv.after
                    out[name] = _shader_value_to_py(val)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _shader_value_to_py(v) -> Any:
    try:
        cols = v.columns
        rows = v.rows
    except Exception:
        return repr(v)
    # Vector / scalar floats and ints
    try:
        result = []
        for c in range(int(cols) if cols else 1):
            row_vals = []
            for r in range(int(rows) if rows else 1):
                vv = v.value
                # vv has fields f32v / s32v / u32v depending on type
                try:
                    f = float(vv.f32v[c * 4 + r])
                    row_vals.append(f)
                except Exception:
                    try:
                        u = int(vv.u32v[c * 4 + r])
                        row_vals.append(u)
                    except Exception:
                        row_vals.append(None)
            result.append(row_vals if rows > 1 else row_vals[0])
        return result if len(result) > 1 else (result[0] if result else None)
    except Exception:
        return repr(v)


def _trace_pixel(controller, event_id: int, x: int, y: int) -> Optional[Any]:
    controller.SetFrameEvent(int(event_id), True)
    inputs = rd.DebugPixelInputs()
    try:
        trace = controller.DebugPixel(int(x), int(y), inputs)
    except Exception:
        trace = None
    return trace


def _walk_states(controller, trace) -> List[Any]:
    """Drain `ContinueDebug` to enumerate every ShaderDebugState."""
    if trace is None or trace.debugger is None:
        return []
    states = []
    try:
        while True:
            chunk = controller.ContinueDebug(trace.debugger)
            if not chunk or len(chunk) == 0:
                break
            for s in chunk:
                states.append(s)
            # Stop when we hit the end (Finished flag) — controller raises empty list when done
            if any((int(s.flags) & int(rd.ShaderEvents.SampleLoadGather)) and False for s in chunk):
                pass
    except Exception:
        pass
    return states


def diff_traces(capture_path: str, left_eid: int, right_eid: int,
                x: int, y: int, *, max_steps: int = 4096) -> Dict[str, Any]:
    cap, controller = _lib.open_capture(capture_path)
    try:
        result: Dict[str, Any] = {
            "leftEventId": left_eid,
            "rightEventId": right_eid,
            "x": x, "y": y,
        }
        trace_l = _trace_pixel(controller, left_eid, x, y)
        if trace_l is None:
            return {**result, "error": "DebugPixel failed on left"}
        states_l = _walk_states(controller, trace_l)[:max_steps]

        trace_r = _trace_pixel(controller, right_eid, x, y)
        if trace_r is None:
            controller.FreeTrace(trace_l)
            return {**result, "error": "DebugPixel failed on right"}
        states_r = _walk_states(controller, trace_r)[:max_steps]

        result["leftStepCount"] = len(states_l)
        result["rightStepCount"] = len(states_r)

        # Walk both traces in lockstep, snapshot registers after each step,
        # compare. Surface the first step where any register's value differs.
        first_divergence = None
        running_l: Dict[str, Any] = {}
        running_r: Dict[str, Any] = {}
        n = min(len(states_l), len(states_r))
        for i in range(n):
            snap_l = _snapshot_registers(states_l[i])
            snap_r = _snapshot_registers(states_r[i])
            for k, v in snap_l.items():
                running_l[k] = v
            for k, v in snap_r.items():
                running_r[k] = v
            # Compare values for keys present in either side
            diffs: Dict[str, Tuple[Any, Any]] = {}
            for key in set(running_l.keys()) | set(running_r.keys()):
                lv = running_l.get(key)
                rv = running_r.get(key)
                if lv != rv:
                    diffs[key] = (lv, rv)
            if diffs and first_divergence is None:
                first_divergence = {
                    "step": i,
                    "instructionLeft": int(states_l[i].nextInstruction) if hasattr(states_l[i], "nextInstruction") else None,
                    "instructionRight": int(states_r[i].nextInstruction) if hasattr(states_r[i], "nextInstruction") else None,
                    "divergentRegisters": {k: {"left": v[0], "right": v[1]} for k, v in list(diffs.items())[:32]},
                }
                # Don't break — let the loop continue so users can inspect
                # the steps that follow.
        result["firstDivergence"] = first_divergence
        result["finalRegisterDelta"] = {
            k: {"left": running_l.get(k), "right": running_r.get(k)}
            for k in sorted(set(running_l.keys()) | set(running_r.keys()))
            if running_l.get(k) != running_r.get(k)
        }
        try:
            controller.FreeTrace(trace_l)
        except Exception:
            pass
        try:
            controller.FreeTrace(trace_r)
        except Exception:
            pass
        return result
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Diff DebugPixel traces between two events.")
    p.add_argument("capture")
    p.add_argument("--left-event", type=int, required=True)
    p.add_argument("--right-event", type=int, required=True)
    p.add_argument("--x", type=int, required=True)
    p.add_argument("--y", type=int, required=True)
    p.add_argument("--max-steps", type=int, default=4096)
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = diff_traces(args.capture, args.left_event, args.right_event,
                          args.x, args.y, max_steps=args.max_steps)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text[:8000] + ("..." if len(text) > 8000 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
