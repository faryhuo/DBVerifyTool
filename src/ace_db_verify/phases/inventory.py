"""Phase 2 - file inventory, entity sets and cross-schema content."""
from __future__ import annotations

import re
from collections import OrderedDict, defaultdict

from ..analysis.file_analyzer import normalized_hash


def category_ranges(entries, section, order):
    res = []
    for t in order:
        ls = [e.line for e in entries if e.section == section and e.stype == t]
        if ls:
            res.append((t, min(ls), max(ls), len(ls)))
    return res


def phase2_inventory(ctx, cfg, entries, disk_entries):
    xml_keys = {e.rel.lower() for e in entries}
    for e in entries:
        if not e.exists:
            ctx.add("inv.missing", "CRITICAL", e.section, e.stype, e.rel, [e.line],
                    "Referenced at XML L%d; resolved to %s" % (e.line, e.abspath))
    for d in disk_entries:
        if d.rel.lower() not in xml_keys:
            ctx.add("inv.extra", "WARNING", d.folder_section, d.stype, d.rel, [], "Not referenced in the changelog.")

    expected = [s.lower() for s in cfg["expected_schemas"]]
    excl = [re.compile(p, re.I) for p in cfg["entity_set_exclude"]]
    groups = OrderedDict()
    for d in disk_entries:
        if d.stype not in cfg["entity_set_types"] or not d.schema:
            continue
        key = (d.folder_section, d.stype, d.base.lower())
        groups.setdefault(key, []).append(d)
    results = []
    for (section, stype, _), items in groups.items():
        present = sorted({d.schema.lower() for d in items})
        if not set(present) & set(expected):
            continue
        base = items[0].base
        if any(p.search(base) for p in excl):
            continue
        missing = [s for s in expected if s not in present]
        variants = [d for d in items if d.schema.lower() in expected and d.fa is not None]
        content = "N/A"
        diff_detail = ""
        if missing:
            content = "N/A (missing %s)" % ", ".join(missing)
        if len(variants) >= 2:
            hashes = defaultdict(list)
            for d in variants:
                hashes[normalized_hash(d.fa, expected)].append(d.schema.lower())
            if len(hashes) == 1:
                content = "MATCH" if not missing else content + "; present variants match"
            else:
                grp = sorted(hashes.values(), key=len, reverse=True)
                diff_detail = " ≠ ".join("/".join(sorted(g)) for g in grp)
                content = "DIFFERENT"
                ctx.add("inv.content", "WARNING", section, stype, base, [],
                        "%s: normalized content differs between schema variants (%s)." % (base, diff_detail))
        if missing:
            ctx.add("inv.entity_set", "CRITICAL", section, stype, base, [],
                    "%s — missing schema(s): %s" % (base, ", ".join(missing)))
        results.append(dict(base=base, section=section, stype=stype, expected=expected, present=present,
                            missing=missing, content=content, diff=diff_detail))
    return results
