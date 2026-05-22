"""Top-N cost analysis — answer "what are the N most expensive draws/dispatches?"

Uses controller.FetchCounters() to collect GPU time + IA samples + clipping
counters per draw/dispatch, then ranks by GPU time (default).

Usage from Python:
    from util.automation import top_n_costs
    rows = top_n_costs.top_n(
        capture_path="path.rdc",
        n=20,
        kind_filter="draw",        # draw / dispatch / copy / all
        sort_by="gpu_time_ns",     # gpu_time_ns / samples_passed / vs_invocations / ps_invocations
        name_regex=None,           # regex against shader entry name
    )
    for r in rows:
        print(r)

CLI:
    python util/automation/top_n_costs.py <capture.rdc> --n 20
    python util/automation/top_n_costs.py <capture.rdc> --n 10 --kind draw --sort gpu_time_ns
    python util/automation/top_n_costs.py <capture.rdc> --json out.json
"""

import argparse
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


# Counter set we collect — best-effort, drivers will return only those they support.
COUNTERS_OF_INTEREST = [
    ("EventGPUDuration",  "gpu_time_ns"),
    ("InputVerticesRead", "ia_vertices"),
    ("VSInvocations",     "vs_invocations"),
    ("PSInvocations",     "ps_invocations"),
    ("CSInvocations",     "cs_invocations"),
    ("SamplesPassed",     "samples_passed"),
    ("RasterizedPrimitives", "rasterized_primitives"),
]


def _resolve_counter_ids(controller):
    """Map our friendly names to the engine's GPUCounter ids actually available."""
    available = controller.EnumerateCounters()
    avail_set = set(int(c) for c in available)
    chosen = []
    for friendly, out_key in COUNTERS_OF_INTEREST:
        try:
            counter_enum = getattr(rd.GPUCounter, friendly)
        except AttributeError:
            continue
        if int(counter_enum) in avail_set:
            chosen.append((counter_enum, out_key))
    return chosen


def _classify_action(action):
    flags = int(action.flags)
    if flags & int(rd.ActionFlags.Drawcall):
        return "draw"
    if flags & int(rd.ActionFlags.Dispatch):
        return "dispatch"
    if flags & int(rd.ActionFlags.Copy):
        return "copy"
    if flags & int(rd.ActionFlags.Resolve):
        return "resolve"
    if flags & int(rd.ActionFlags.Clear):
        return "clear"
    return "other"


def collect(capture_path: str, kind_filter: str = "all",
            name_regex: str = None) -> list:
    """Collect per-event cost rows. Returns a list of dicts."""
    cap, controller = _lib.open_capture(capture_path)
    try:
        counter_set = _resolve_counter_ids(controller)
        if not counter_set:
            print("WARNING: no GPU counters available; gpu_time will be None")
        counter_ids = [c for c, _ in counter_set]
        # Fetch all counters in one call
        results = controller.FetchCounters(counter_ids) if counter_ids else []
        # Map eventId -> { friendly_key: value }
        per_event = {}
        for r in results:
            eid = int(r.eventId)
            counter_enum = r.counter
            value = r.value
            # value is a union — pick a scalar
            for c, k in counter_set:
                if int(c) == int(counter_enum):
                    out_key = k
                    break
            else:
                continue
            try:
                v = float(value.f64)
                if v == 0.0:
                    v = float(value.u64)
            except Exception:
                try: v = float(value.u64)
                except Exception:
                    try: v = float(value.u32)
                    except Exception: v = None
            per_event.setdefault(eid, {})[out_key] = v
        # Walk actions and build the cost table
        rx = re.compile(name_regex) if name_regex else None
        rows = []
        for a in _lib.walk_actions(controller):
            kind = _classify_action(a)
            if kind_filter != "all" and kind != kind_filter:
                continue
            eid = int(a.eventId)
            # Get shader entry for naming
            entry = None
            ps_hash_short = None
            try:
                controller.SetFrameEvent(eid, True)
                pipe = controller.GetPipelineState()
                for stage_enum, _ in (
                    (rd.ShaderStage.Compute, None),
                    (rd.ShaderStage.Pixel, None),
                    (rd.ShaderStage.Vertex, None),
                ):
                    refl = pipe.GetShaderReflection(stage_enum)
                    if refl and len(refl.rawBytes) > 0:
                        entry = str(refl.entryPoint)
                        ps_hash_short = _lib.shader_bytecode_hash(bytes(refl.rawBytes))[:16]
                        break
            except Exception:
                pass
            if rx and entry and not rx.search(entry):
                continue
            try:
                action_name = str(a.GetName(controller.GetStructuredFile()))
            except Exception:
                action_name = "?"
            counters = per_event.get(eid, {})
            row = {
                "eventId": eid,
                "kind": kind,
                "action_name": action_name,
                "entry_point": entry,
                "shader_hash": ps_hash_short,
            }
            row.update(counters)
            rows.append(row)
        return rows
    finally:
        controller.Shutdown()
        cap.Shutdown()


def top_n(capture_path: str, n: int = 20, kind_filter: str = "all",
          name_regex: str = None, sort_by: str = "gpu_time_ns",
          rows: list = None) -> list:
    if rows is None:
        rows = collect(capture_path, kind_filter, name_regex)
    rows = [r for r in rows if r.get(sort_by) is not None]
    rows.sort(key=lambda r: r.get(sort_by) or 0, reverse=True)
    return rows[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--kind", default="all",
                     choices=["all", "draw", "dispatch", "copy", "resolve", "clear", "other"])
    ap.add_argument("--name-regex")
    ap.add_argument("--sort", default="gpu_time_ns",
                     choices=["gpu_time_ns", "ia_vertices", "vs_invocations",
                              "ps_invocations", "cs_invocations", "samples_passed",
                              "rasterized_primitives"])
    ap.add_argument("--json", help="dump full result to JSON file")
    args = ap.parse_args()

    rows = top_n(args.capture, args.n, args.kind, args.name_regex, args.sort)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=2, default=str)
        print(f"wrote {args.json}")

    print(f"\nTop {args.n} {args.kind} events by {args.sort}:\n")
    print(f"{'#':>3}  {'eventId':>7}  {args.sort:>14}  {'kind':>8}  entry / action")
    for i, r in enumerate(rows, 1):
        v = r.get(args.sort)
        v_str = f"{v:14.0f}" if v is not None else f"{'?':>14}"
        entry = r.get("entry_point") or r.get("action_name", "?")
        print(f"{i:>3}  {r['eventId']:>7}  {v_str}  {r['kind']:>8}  {entry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
