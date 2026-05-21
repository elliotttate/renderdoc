"""Per-event GPU performance counters — Nsight Range Profiler equivalent.

Useful for "is the right eye doing 2x the work of the left eye, or 0x?"
which is often the first signal that a draw is being skipped or doubled.

Wraps controller.FetchCounters(). By default fetches event GPU duration +
basic ALU/sample/raster counters, depending on what the local GPU exposes.

Usage:
    python -m util.automation.perf_counters <cap.rdc> [--counters EventGPUDuration,ALU,RasterizedPrimitives] [--out file]
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


DEFAULT_COUNTERS = ["EventGPUDuration"]


def _resolve_counter(name: str):
    return getattr(rd.GPUCounter, name, None)


def fetch(capture: str, counter_names) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        available = list(controller.EnumerateCounters()) if hasattr(controller, "EnumerateCounters") else []
        wanted = []
        for n in counter_names or DEFAULT_COUNTERS:
            c = _resolve_counter(n)
            if c is None:
                continue
            wanted.append(c)
        if not wanted:
            return {"error": "no resolved counters", "availableCount": len(available)}
        results = controller.FetchCounters(wanted)

        # Index by eventId -> {counterName: value}
        by_event = {}
        for r in results:
            eid = int(r.eventId)
            cname = str(r.counter).split(".")[-1]
            val = None
            try:
                val = float(r.value.d)
                if val == 0.0:
                    val = float(r.value.f)
            except Exception:
                try:
                    val = int(r.value.u64)
                except Exception:
                    val = None
            by_event.setdefault(eid, {})[cname] = val

        rows = sorted([{"eventId": e, **v} for e, v in by_event.items()], key=lambda r: r["eventId"])
        return {
            "counters": [str(c).split(".")[-1] for c in wanted],
            "rowCount": len(rows),
            "rows": rows[:10000],
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Per-event GPU counter dump.")
    p.add_argument("capture")
    p.add_argument("--counters", help="Comma-separated GPUCounter names (default: EventGPUDuration)")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    names = args.counters.split(",") if args.counters else DEFAULT_COUNTERS

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = fetch(args.capture, names)
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
