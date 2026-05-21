"""UEVR ingest and correlation (§7, §17 of the roadmap).

Reads sidecar JSON dropped by UEVR (override manifest, runtime status, eye
sample logs, hook-mode flags), and attaches eye + override metadata to events
in an existing index_capture output. Produces a uevr.jsonl alongside the index
that can be joined to state.jsonl by eventId.

Schema this expects (any subset is OK):

  manifest.json  ->  {"overrides": [{"target_hash": "166dba88", "replacement_hash": "abcd...",
                                     "transform_ops": 12, "enabled": true}, ...]}
  status.json    ->  {"profile": "...", "uevr_active": true, "hook_mode": "frontend",
                      "stereo_mode": "sbs", "swap_eyes": false}
  events.json    ->  {"events": [{"eventId": 16042, "eye": "right", "tag": "magenta_probe"}, ...]}
  build.json     ->  {"exe": "...", "cmdline": "...", "build": "..."}

Usage:
    python -m util.automation.uevr_ingest <index_dir> --uevr-dir <dir-with-jsons> [--out uevr.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _read_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def ingest(index_dir: str, uevr_dir: str) -> dict:
    manifest = _read_json(os.path.join(uevr_dir, "manifest.json")) or {}
    status = _read_json(os.path.join(uevr_dir, "status.json")) or {}
    annotated_events = _read_json(os.path.join(uevr_dir, "events.json")) or {}
    build_info = _read_json(os.path.join(uevr_dir, "build.json")) or {}

    # Build a hash -> override entry index
    override_by_target = {}
    for o in manifest.get("overrides", []):
        h = o.get("target_hash")
        if h:
            override_by_target[h.lower()] = o

    # Per-event UEVR annotation merged with index state
    state = {s["eventId"]: s for s in _read_jsonl(os.path.join(index_dir, "state.jsonl"))}
    explicit_event_anno = {e["eventId"]: e for e in annotated_events.get("events", [])}

    rows = []
    for eid in sorted(state.keys()):
        s = state[eid]
        shader_hashes = [sh.get("bytecodeHash") for sh in s.get("shaders", []) if sh.get("bytecodeHash")]
        # An override is "expected" at this event if any bound shader hash matches a target.
        active = []
        for h in shader_hashes:
            ov = override_by_target.get((h or "").lower())
            if ov is not None:
                active.append(ov)
        anno = explicit_event_anno.get(eid, {})
        rows.append({
            "eventId": eid,
            "eye": anno.get("eye"),
            "tag": anno.get("tag"),
            "boundShaderHashes": shader_hashes,
            "matchingOverrides": active,
            "override_expected": len(active) > 0,
        })

    summary = {
        "uevr_status": status,
        "build_info": build_info,
        "override_count": len(manifest.get("overrides", [])),
        "events_with_matching_override": sum(1 for r in rows if r["override_expected"]),
    }
    return {"summary": summary, "rows": rows}


def write_uevr_jsonl(index_dir: str, ingest_result: dict, out_path=None):
    out_path = out_path or os.path.join(index_dir, "uevr.jsonl")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        # First line is summary as a single object
        f.write(json.dumps({"_summary": ingest_result["summary"]}, ensure_ascii=False) + "\n")
        for r in ingest_result["rows"]:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    return out_path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ingest UEVR sidecar JSON and correlate to an index.")
    p.add_argument("index_dir", help="Directory produced by index_capture")
    p.add_argument("--uevr-dir", required=True, help="Directory containing manifest.json/status.json/etc.")
    p.add_argument("--out", "-o", help="Output file (default: <index_dir>/uevr.jsonl)")
    args = p.parse_args(argv)

    result = ingest(args.index_dir, args.uevr_dir)
    out = write_uevr_jsonl(args.index_dir, result, args.out)
    print(json.dumps({"out": out, "summary": result["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
