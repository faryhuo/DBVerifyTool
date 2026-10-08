"""Phases 4-6 - deploy and rollback dependency ordering."""
from __future__ import annotations

from collections import OrderedDict, defaultdict

from ..constants import TYPE_LABEL


def build_index(entries, section):
    idx = defaultdict(list)
    by_name = defaultdict(set)
    for e in entries:
        if e.section == section and e.stype in ("Table", "Tvf", "Procedure", "Data") and e.schema:
            idx[(e.schema.lower(), e.obj.lower())].append(e)
            by_name[e.obj.lower()].add(e.schema.lower())
    return idx, by_name


def resolve(schema, name, file_schema, by_name):
    nm = name.lower()
    if nm not in by_name:
        return None
    schemas = by_name[nm]
    if schema is not None:
        s = schema.lower()
        return (s, nm) if s in schemas else None
    for s in (file_schema.lower(), "dbo"):
        if s in schemas:
            return (s, nm)
    if len(schemas) == 1:
        return (next(iter(schemas)), nm)
    return None


def deps_of(e, idx, by_name):
    out = OrderedDict()
    if not e.fa:
        return []
    for schema, name, line in e.fa.refs:
        key = resolve(schema, name, e.schema, by_name)
        if not key:
            continue
        for x in idx[key]:
            if x is e:
                continue
            if e.stype == "Data":
                if x.stype not in ("Table", "Data"):
                    continue
            elif x.stype not in ("Table", "Tvf", "Procedure"):
                continue
            if e.stype != "Data" and x.obj.lower() == e.obj.lower() and x.schema.lower() == e.schema.lower():
                continue
            if x.rel not in out:
                out[x.rel] = (x, line)
    return list(out.values())


def phase45_dependencies(ctx, entries):
    d_idx, d_names = build_index(entries, "Deploy")
    r_idx, r_names = build_index(entries, "Rollback")
    deploy = [e for e in entries if e.section == "Deploy"]
    rollback = [e for e in entries if e.section == "Rollback"]
    for y in deploy:
        y.deps = deps_of(y, d_idx, d_names)
        for x, line in y.deps:
            if x.line > y.line:
                weak = y.stype == "Procedure" or (x.fa is not None and x.fa.main_op == "ALTER")
                ctx.add("dep.order", "WARNING" if weak else "CRITICAL", "Deploy", y.stype, y.rel, [line],
                        "%s (XML L%d) references %s.%s (%s) at L%d, but that script is deployed later (XML L%d)."
                        % (y.obj, y.line, x.schema, x.obj, TYPE_LABEL[x.stype], line, x.line))
    by_tail_rb = {e.tail: e for e in rollback}
    for y in rollback:
        deps = OrderedDict((x.rel, (x, line)) for x, line in deps_of(y, r_idx, r_names))
        # dependencies known from the deploy side
        dy = next((d for d in deploy if d.tail == y.tail), None)
        if dy:
            for x, line in dy.deps:
                rx = by_tail_rb.get(x.tail)
                if rx is not None and rx is not y and rx.rel not in deps:
                    deps[rx.rel] = (rx, 0)
        y.deps = list(deps.values())
        for x, line in y.deps:
            if x.line >= y.line or x.fa is None:
                continue
            destructive = x.stype == "Data" or x.fa.main_op == "DROP"
            if not destructive:
                continue
            what = "removes its rows" if x.stype == "Data" else "drops it"
            ctx.add("rb.order", "WARNING" if y.stype == "Procedure" else "CRITICAL", "Rollback", y.stype, y.rel,
                    [line] if line else [],
                    "Rollback of %s (XML L%d) depends on %s.%s (%s), whose rollback %s earlier at XML L%d."
                    % (y.obj, y.line, x.schema, x.obj, TYPE_LABEL[x.stype], what, x.line))
