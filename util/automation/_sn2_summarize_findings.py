"""Consume the JSON artefacts from _sn2_full_investigation.py and emit a
human-readable findings report.

Reads from $SN2_INVESTIGATION_OUT (default E:/tmp_dir/sn2_invest_full),
writes the report to $SN2_FINDINGS_OUT (default
E:/tmp_dir/sn2_invest_full/FINDINGS.md).

The report is structured as:

  - Per-eye event counts
  - Top divergent pairs with their breakdown
  - Compute-writer findings for right-eye t5 (the SN2 fog volume)
  - Heap heatmap mismatch count
  - First divergent shader instruction (from debug_pixel_pair)
  - Fix proposal summary
  - Candidate IDA targets (from descriptor copy log callstacks if any)
"""

import argparse
import json
import os
import sys


def _load(p):
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _bullet(label, value, lines):
    lines.append(f"- **{label}:** {value}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dir",
        default=os.environ.get("SN2_INVESTIGATION_OUT", r"E:\tmp_dir\sn2_invest_full"),
    )
    ap.add_argument(
        "--out",
        default=os.environ.get("SN2_FINDINGS_OUT", ""),
    )
    args = ap.parse_args(argv)
    d = args.dir
    out_path = args.out or os.path.join(d, "FINDINGS.md")

    lines = []
    lines.append("# SN2 Right-Eye Investigation — Findings")
    lines.append("")
    lines.append(f"Artefact directory: `{d}`")
    lines.append("")

    # Eye classification
    eye = _load(os.path.join(d, "a_eye_classification.json"))
    lines.append("## A. Eye classification")
    lines.append("")
    if eye is None:
        lines.append("_eye classification artefact missing_")
    else:
        per_eye = eye.get("perEyeCounts") or {}
        _bullet("left", per_eye.get("left", 0), lines)
        _bullet("right", per_eye.get("right", 0), lines)
        _bullet("unknown", per_eye.get("unknown", 0), lines)
        size = eye.get("fullSize") or []
        if size:
            _bullet("inferred main RT size", f"{size[0]} x {size[1]}", lines)
        lines.append("")

    # Pairs
    pairs = _load(os.path.join(d, "a_event_pairs.json"))
    lines.append("## A. Matched L/R event pairs")
    lines.append("")
    if pairs is None:
        lines.append("_pair artefact missing_")
    else:
        _bullet("pair count", len(pairs.get("pairs", [])), lines)
        lines.append("")

    # Divergence top-N
    div = _load(os.path.join(d, "b_stereo_divergence.json"))
    lines.append("## B. Top divergence")
    lines.append("")
    if div is None:
        lines.append("_divergence artefact missing_")
    else:
        _bullet("pairs scored", div.get("scoredPairs", 0), lines)
        lines.append("")
        for i, p in enumerate(div.get("top", []), start=1):
            lines.append(f"### Pair #{i} — score {p.get('score', 0):.2f}")
            lines.append("")
            _bullet("left event", p.get("leftEventId"), lines)
            _bullet("right event", p.get("rightEventId"), lines)
            bd = p.get("breakdown") or {}
            if "rightDispatchDead" in bd:
                lines.append("- **RIGHT DISPATCH DEAD (X=0):** ⚠️ this is the SN2 fog-volume pattern")
            if "rtResourceDelta" in bd:
                _bullet("RT resource delta", "different RT bound between eyes", lines)
            if "shaderHashDeltas" in bd:
                _bullet("shader hash deltas", json.dumps(bd["shaderHashDeltas"])[:300], lines)
            if "bindingDeltas" in bd:
                lines.append(f"- **binding deltas:** {len(bd['bindingDeltas'])} entries")
                for bdl in bd["bindingDeltas"][:5]:
                    lines.append(f"  - stage={bdl.get('stage')} reg={bdl.get('register')} type={bdl.get('type')}: L={bdl.get('left')} R={bdl.get('right')}")
            if "dispatchDimensionDelta" in bd:
                _bullet("dispatch dim delta", str(bd["dispatchDimensionDelta"]), lines)
            if "rtMinMaxDelta" in bd:
                _bullet("RT min/max delta", f"{bd['rtMinMaxDelta']:.3f}", lines)
            lines.append("")

    # Compute writers
    cw = _load(os.path.join(d, "c2_compute_writers_t5.json"))
    lines.append("## C. Right-eye PS t5 writers")
    lines.append("")
    if cw is None:
        lines.append("_compute writers artefact missing_")
    else:
        s = cw.get("summary", {})
        _bullet("resource", s.get("resource"), lines)
        _bullet("total writers", s.get("writeCount", 0), lines)
        _bullet("dead dispatches", s.get("deadDispatches", 0), lines)
        if s.get("perEyeWrites"):
            _bullet("per-eye writes", str(s["perEyeWrites"]), lines)
        if s.get("deadPerEye"):
            _bullet("dead per eye", str(s["deadPerEye"]), lines)
        # Highlight the dead ones if any
        dead = [w for w in cw.get("writes", []) if w.get("dead")]
        if dead:
            lines.append("")
            lines.append("**Dead-dispatch writers (X=0):**")
            for w in dead[:5]:
                lines.append(f"- event {w.get('eventId')} ({w.get('name')}): eye={w.get('eye')}  dim={w.get('dispatchDimension')}")
        lines.append("")

    # Heap heatmap
    heat = _load(os.path.join(d, "d_descriptor_heap_heatmap_mismatch.json"))
    lines.append("## D. Descriptor heap heatmap (cross-eye mismatches)")
    lines.append("")
    if heat is None:
        lines.append("_heatmap artefact missing_")
    else:
        s = heat.get("summary", {})
        _bullet("total slots scanned", s.get("totalSlots", 0), lines)
        _bullet("mismatched slots", s.get("mismatches", 0), lines)
        _bullet("heaps touched", s.get("heapsTouched", 0), lines)
        mm = heat.get("mismatchRows") or []
        if mm:
            lines.append("")
            lines.append("**Top 5 mismatched slots:**")
            for row in mm[:5]:
                lines.append(
                    f"- heap={row.get('heap')} slot={row.get('slot')}: "
                    f"L wrote ({row.get('lastWrittenEventId')}) → R consumed ({row.get('lastConsumedEventId')})"
                )
        lines.append("")

    # Debug pixel pair
    dbg = _load(os.path.join(d, "g_debug_pixel_pair.json"))
    lines.append("## G. Shader trace divergence")
    lines.append("")
    if dbg is None or dbg.get("error"):
        lines.append("_pixel debugger artefact missing or DebugPixel failed_")
    else:
        fd = dbg.get("firstDivergence")
        if fd:
            _bullet("first divergent step", fd.get("step"), lines)
            _bullet("left instruction index", fd.get("instructionLeft"), lines)
            _bullet("right instruction index", fd.get("instructionRight"), lines)
            divs = fd.get("divergentRegisters") or {}
            if divs:
                lines.append("")
                lines.append("**First divergent registers:**")
                for k, v in list(divs.items())[:8]:
                    lines.append(f"- `{k}`: L={v.get('left')} R={v.get('right')}")
        else:
            lines.append("No register-level divergence detected. Either the trace is the same, "
                         "or DebugPixel reported no useful changes for the sampled coordinate.")
    lines.append("")

    # Fix proposal
    prop = _load(os.path.join(d, "e_fix_proposal_top.json"))
    lines.append("## E. Fix proposals")
    lines.append("")
    if prop is None:
        lines.append("_proposal artefact missing_")
    else:
        _bullet("proposals", prop.get("proposalCount", 0), lines)
        for i, pr in enumerate(prop.get("proposals", []), start=1):
            lines.append(f"### Proposal #{i} — {pr.get('kind')}")
            lines.append(f"  {pr.get('suggestion', '')}")
            lines.append("")

    # Descriptor copy log callstack hints
    cl = _load(os.path.join(d, "f_descriptor_copy_log.json"))
    lines.append("## F. Descriptor write log")
    lines.append("")
    if cl is None:
        lines.append("_copy log artefact missing_")
    else:
        _bullet("total chunks", cl.get("total", 0), lines)
        _bullet("by kind", str(cl.get("summary", {})), lines)
        with_callstack = [c for c in cl.get("events", []) if c.get("callstack")]
        if with_callstack:
            _bullet("chunks with callstacks", len(with_callstack), lines)
            lines.append("")
            lines.append("**Sample callstacks (first 3):**")
            for c in with_callstack[:3]:
                lines.append(f"- chunk #{c.get('chunkIndex')} {c.get('name')}:")
                for frame in (c.get('callstack') or [])[:6]:
                    lines.append(f"    - 0x{frame:016x}")
        else:
            lines.append("_(no callstack data in this capture — re-capture with "
                         "`captureCallstacks=true` to get the engine function names "
                         "for descriptor writes)_")
        lines.append("")

    text = "\n".join(lines) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {out_path} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
