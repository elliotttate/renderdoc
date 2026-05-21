"""Capture diff (§11 of the roadmap).

Compares two indexed captures and emits a structured delta:
  - per-event flag/name divergence
  - PSO/shader-hash usage deltas
  - action count deltas
  - first divergent event

Operates over the index_capture output (the .index/ directories) so it's cheap
to run and language-agnostic.

Usage:
    python -m util.automation.capture_diff <before.index> <after.index> [--out diff.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter


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


def _read_meta(idx_dir):
    p = os.path.join(idx_dir, "meta.json")
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def diff_captures(before: str, after: str) -> dict:
    a_events = {e["eventId"]: e for e in _read_jsonl(os.path.join(before, "events.jsonl"))}
    b_events = {e["eventId"]: e for e in _read_jsonl(os.path.join(after, "events.jsonl"))}

    a_actions = {a["eventId"]: a for a in _read_jsonl(os.path.join(before, "actions.jsonl"))}
    b_actions = {a["eventId"]: a for a in _read_jsonl(os.path.join(after, "actions.jsonl"))}

    a_state = {s["eventId"]: s for s in _read_jsonl(os.path.join(before, "state.jsonl"))}
    b_state = {s["eventId"]: s for s in _read_jsonl(os.path.join(after, "state.jsonl"))}

    common_eids = sorted(set(a_events.keys()) & set(b_events.keys()))
    only_in_before = sorted(set(a_events.keys()) - set(b_events.keys()))
    only_in_after = sorted(set(b_events.keys()) - set(a_events.keys()))

    first_divergent = None
    name_divergences = []
    flag_divergences = []
    pso_divergences = []
    shader_divergences = []
    binding_divergences = []

    for eid in common_eids:
        a, b = a_events[eid], b_events[eid]
        if a.get("name") != b.get("name"):
            if first_divergent is None:
                first_divergent = eid
            name_divergences.append({"eventId": eid, "before": a.get("name"), "after": b.get("name")})
        if a.get("flags") != b.get("flags"):
            if first_divergent is None:
                first_divergent = eid
            flag_divergences.append({"eventId": eid, "before": a.get("flags"), "after": b.get("flags")})

        sa, sb = a_state.get(eid), b_state.get(eid)
        if sa and sb:
            if sa.get("pipelineId") != sb.get("pipelineId"):
                pso_divergences.append({"eventId": eid, "before": sa.get("pipelineId"), "after": sb.get("pipelineId")})
            ah = tuple(s.get("bytecodeHash") for s in sa.get("shaders", []))
            bh = tuple(s.get("bytecodeHash") for s in sb.get("shaders", []))
            if ah != bh:
                shader_divergences.append({"eventId": eid, "before": list(ah), "after": list(bh)})
            ab = _binding_key(sa.get("bindings", []))
            bb = _binding_key(sb.get("bindings", []))
            if ab != bb:
                binding_divergences.append({
                    "eventId": eid,
                    "addedOrChanged": list(bb - ab),
                    "removed": list(ab - bb),
                })

    # PSO usage counts
    a_psos = Counter(s.get("pipelineId") for s in a_state.values() if s.get("pipelineId"))
    b_psos = Counter(s.get("pipelineId") for s in b_state.values() if s.get("pipelineId"))
    pso_count_delta = []
    for k in set(a_psos.keys()) | set(b_psos.keys()):
        if a_psos[k] != b_psos[k]:
            pso_count_delta.append({"pipelineId": k, "before": a_psos[k], "after": b_psos[k]})

    return {
        "before": {"dir": before, "meta": _read_meta(before)},
        "after": {"dir": after, "meta": _read_meta(after)},
        "firstDivergentEvent": first_divergent,
        "eventCount": {"before": len(a_events), "after": len(b_events)},
        "actionCount": {"before": len(a_actions), "after": len(b_actions)},
        "onlyInBefore": only_in_before[:50],
        "onlyInAfter": only_in_after[:50],
        "nameDivergences": name_divergences[:200],
        "flagDivergences": flag_divergences[:200],
        "psoDivergences": pso_divergences[:200],
        "shaderDivergences": shader_divergences[:200],
        "psoUsageDelta": pso_count_delta,
        "bindingDivergences": binding_divergences[:200],
    }


def _binding_key(bindings):
    """A hashable representation of bindings, keyed by stage/type/register/resource."""
    out = set()
    for b in bindings:
        out.add(
            (
                b.get("stage"),
                b.get("type"),
                b.get("register"),
                b.get("space"),
                b.get("resource"),
            )
        )
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Diff two indexed captures.")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    out = diff_captures(args.before, args.after)
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
