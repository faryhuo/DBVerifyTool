"""Phase 3 - deploy/rollback pairing, rollback logic and schema references."""
from __future__ import annotations

import re
from collections import OrderedDict

from ..lexer import TYPE_KINDS


def phase3_verification(ctx, cfg, entries):
    dep = {e.tail: e for e in entries if e.section == "Deploy" and e.folder_section == "Deploy"}
    rb = {e.tail: e for e in entries if e.section == "Rollback" and e.folder_section == "Rollback"}

    def counterpart(e, to):
        parts = e.rel.split("/")
        low = [p.lower() for p in parts]
        k = low.index(e.folder_section.lower())
        return "/".join(parts[:k] + [to] + parts[k + 1:])

    for t, e in dep.items():
        if t not in rb:
            ctx.add("ver.pairing", "CRITICAL", "Deploy", e.stype, e.rel, [e.line],
                    "Deploy script (XML L%d) has no rollback counterpart %s in the <rollback> section."
                    % (e.line, counterpart(e, "Rollback")))
    for t, e in rb.items():
        if t not in dep:
            ctx.add("ver.pairing", "WARNING", "Rollback", e.stype, e.rel, [e.line],
                    "Rollback script (XML L%d) has no deploy counterpart %s." % (e.line, counterpart(e, "Deploy")))
    pre_objs = {o.lower() for o in cfg["preexisting_objects"]}
    coa = set(x.upper() for x in cfg["create_or_alter_rollback"])
    restore = {"ALTER", "CREATE OR ALTER", "RECREATE"}
    pairs = []
    for t, d in dep.items():
        r = rb.get(t)
        if not r or not d.fa or not r.fa:
            continue
        pairs.append((d, r))
        if d.stype in ("Tvf", "Procedure"):
            pre = d.fa.preexisting_marker or d.obj.lower() in pre_objs or ("%s.%s" % (d.schema, d.obj)).lower() in pre_objs
            allowed = {
                "CREATE": {"DROP"},
                "CREATE OR ALTER": restore if pre else coa,
                "ALTER": restore,
                "RECREATE": {"DROP"} | restore,
                "DROP": {"CREATE", "CREATE OR ALTER", "RECREATE"},
            }
            if d.fa.main_op == "NONE":
                ctx.add("ver.op_unknown", "WARNING", "Deploy", d.stype, d.rel, [],
                        "No CREATE/ALTER/DROP %s %s found in the deploy script." % (TYPE_KINDS[d.stype][0], d.obj))
                continue
            if r.fa.main_op == "NONE":
                ctx.add("ver.op_unknown", "WARNING", "Rollback", r.stype, r.rel, [],
                        "No CREATE/ALTER/DROP %s %s found in the rollback script." % (TYPE_KINDS[r.stype][0], r.obj))
                continue
            if r.fa.main_op not in allowed[d.fa.main_op]:
                exp = " / ".join(sorted(allowed[d.fa.main_op]))
                ctx.add("ver.rollback_logic", "CRITICAL", "Deploy", d.stype, d.rel, [],
                        "Deploy operation is %s but rollback is %s; expected rollback to %s."
                        % (d.fa.main_op, r.fa.op_display, exp))
        elif d.stype == "Table":
            key = d.obj.lower()
            if d.fa.main_op in ("CREATE", "CREATE OR ALTER") and r.fa.main_op not in ("DROP", "RECREATE"):
                ctx.add("ver.rollback_logic", "CRITICAL", "Deploy", d.stype, d.rel, [],
                        "Deploy creates table %s but rollback does not drop it (rollback: %s)." % (d.obj, r.fa.op_display))
            added = [c for c in d.fa.added_cols.get(key, [])]
            if added:
                dropped = {c.lower() for c in r.fa.dropped_cols.get(key, [])}
                miss = [c for c in added if c.lower() not in dropped]
                if miss and r.fa.main_op not in ("DROP", "RECREATE"):
                    ctx.add("ver.rollback_logic", "WARNING", "Deploy", d.stype, d.rel, [],
                            "Deploy adds column(s) %s to %s but rollback has no DROP COLUMN for them."
                            % (", ".join(miss), d.obj))
        elif d.stype == "Data":
            if ("INSERT" in d.fa.dml or "MERGE" in d.fa.dml) and not set(r.fa.dml) & {"DELETE", "TRUNCATE", "UPDATE", "MERGE"}:
                ctx.add("ver.rollback_logic", "WARNING", "Deploy", d.stype, d.rel, [],
                        "Deploy inserts data but the rollback script has no DELETE/UPDATE/MERGE/TRUNCATE.")
    # schema mismatch
    expected = {s.lower() for s in cfg["expected_schemas"]}
    allow = cfg["cross_schema_allow"]
    for e in entries:
        if not e.fa or not e.schema:
            continue
        fs = e.schema.lower()
        found = OrderedDict()
        for schema, name, line in e.fa.refs:
            if not schema:
                continue
            s = schema.lower()
            if s in expected and s != fs:
                ok = False
                for a in allow:
                    if a.get("from", "").lower() == fs and a.get("to", "").lower() == s \
                            and re.search(a.get("file_regex", ""), e.rel, re.I) \
                            and re.search(a.get("object_regex", ""), name, re.I):
                        ok = True
                        break
                if not ok:
                    found.setdefault((schema, name), []).append(line)
        for (schema, name), lns in found.items():
            ctx.add("ver.schema_mismatch", "CRITICAL", e.section, e.stype, e.rel, lns,
                    "Object reference [%s].[%s] in a [%s] file (expected schema: [%s])." % (schema, name, e.schema, e.schema))
    return pairs
