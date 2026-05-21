"""Follow-up query against the artefacts that `_run_sn2_investigation.py`
produced. Identifies the exact CreateShaderResourceView / CopyDescriptors
chunk that wrote the wrong resource into the bindless slot the right-eye
basepass consumes.

This is the question the May 16 investigation couldn't answer with stock
RenderDoc:

> Which engine call wrote ResourceId X into heap H slot N?

With the chunk-level d3d12_copy_descriptors log plus the per-action
descriptor_history poll, we can answer it now. The answer's chunk metadata
(threadID, timestampMicro, callstack if present) is the new entry point
for an IDA hunt for FRDGBuilder::Execute -> SetRHI -> CreateShaderResourceView.

Run this AFTER _run_sn2_investigation.py has produced its JSON artefacts.
"""

import argparse
import json
import os
import sys


def _load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dir",
        default=os.environ.get("SN2_INVESTIGATION_OUT", r"E:\tmp_dir\sn2_invest_real"),
        help="Directory containing the JSON artefacts from _run_sn2_investigation.py",
    )
    args = ap.parse_args(argv)
    d = args.dir

    # 1. Identify the t5 binding on the right-eye basepass draw.
    right_hits = _load(os.path.join(d, "right_eye_basepass_hits.json"))
    if not right_hits:
        print("No right-eye basepass hits in artefacts. Run _run_sn2_investigation.py first.")
        sys.exit(1)
    target_eid = right_hits[0]["eventId"]
    print(f"Right-eye basepass target event: {target_eid}")

    state_path = os.path.join(d, f"state_at_event_{target_eid}.json")
    state = _load(state_path)

    t5 = None
    for b in state.get("bindings", []):
        if b.get("stage") == "Pixel" and b.get("register") == 5 and \
                (b.get("type") or "").startswith("Read"):
            t5 = b
            break
    if t5 is None:
        print("No PS t5 binding found at right-eye basepass event.")
        sys.exit(2)

    print(f"PS t5 binding @ event {target_eid}:")
    for k in ("type", "register", "space", "resource", "view", "heap", "heapByteOffset", "byteSize", "name"):
        if k in t5:
            print(f"  {k}: {t5[k]}")

    target_heap = t5.get("heap")
    target_offset = t5.get("heapByteOffset")
    target_resource = t5.get("resource")
    if not target_heap or target_offset is None:
        print("Bindless slot not resolvable; nothing to search for.")
        sys.exit(3)

    # 2. Search the chunk-based copy log for descriptor writes hitting this
    #    heap+slot.
    copy_log = _load(os.path.join(d, "descriptor_copy_log.json"))
    events = copy_log.get("events", [])
    matches = []
    for c in events:
        args_ = c.get("args", {}) or {}
        # The chunk fields differ per chunk kind. Both Create*View and
        # CopyDescriptors[Simple] expose a destination handle via either
        # `dst` (PortableHandle) or by being nested in DescriptorCopies.
        def walk(d, key):
            if isinstance(d, dict):
                if "heap" in d and "index" in d:
                    yield d.get("heap"), d.get("index")
                else:
                    for v in d.values():
                        yield from walk(v, key)
            elif isinstance(d, list):
                for x in d:
                    yield from walk(x, key)

        for heap_val, index_val in walk(args_, "dst"):
            if heap_val == target_heap and index_val == target_offset:
                matches.append(c)
                break

    print(f"\nDescriptor-write chunks targeting heap={target_heap} slot={target_offset}:")
    if not matches:
        print(f"  (none found via direct chunk match; check the descriptor_history.json "
              f"fallback for the resolved write).")
    for m in matches:
        print(f"  chunk #{m.get('chunkIndex')}: {m.get('name')}  "
              f"thread={m.get('threadID')}  t_us={m.get('timestampMicro')}")
        for k, v in (m.get("args") or {}).items():
            if isinstance(v, (str, int, float)) or v is None:
                print(f"    {k}: {v}")

    # 3. Cross-check via the polled descriptor_history.
    hist = _load(os.path.join(d, "descriptor_history.json"))
    writes = hist.get("writes", [])
    last_before = None
    for w in writes:
        if w.get("heap") != target_heap or w.get("slot") != target_offset:
            continue
        if int(w.get("eventId", -1)) > target_eid:
            break
        last_before = w
    print(f"\nLast resolved write to heap={target_heap} slot={target_offset} "
          f"before event {target_eid}:")
    if last_before is None:
        print("  (none)")
    else:
        for k in ("eventId", "type", "resource", "view", "firstMip", "numMips",
                  "firstSlice", "numSlices", "format"):
            if k in last_before:
                print(f"  {k}: {last_before[k]}")

    # 4. Resource lineage for the bound resource.
    if target_resource:
        lin_path = os.path.join(d, f"resource_lineage_{target_resource.replace(':', '_')}.json")
        if os.path.exists(lin_path):
            lin = _load(lin_path)
            print(f"\nResource lineage for {target_resource}:")
            for k in ("usageCount", "writeCount", "readCount", "lastWriterBefore"):
                if k in lin:
                    print(f"  {k}: {lin[k]}")
        else:
            print(f"\n(no lineage artefact for {target_resource})")


if __name__ == "__main__":
    main()
