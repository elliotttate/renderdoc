"""Print machine-readable pipeline state at a specific event.

Usage:
    python -m util.automation.state_at_event <capture.rdc> --event <eid>
"""

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


def state_at_event(capture_path: str, event_id: int) -> dict:
    cap, controller = _lib.open_capture(capture_path)
    try:
        return _lib.collect_state_at_event(controller, event_id)
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Dump pipeline state at a specific event ID.")
    p.add_argument("capture", help="Path to .rdc capture file")
    p.add_argument("--event", "-e", type=int, required=True, help="Event ID to inspect")
    p.add_argument("--out", "-o", help="Optional output file (otherwise stdout)")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        state = state_at_event(args.capture, args.event)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(state, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
