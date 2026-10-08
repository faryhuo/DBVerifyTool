"""Per-batch static compilation checks."""
from __future__ import annotations

from .reader import find_routine_headers
from ..lexer import DECL_STOP, kw, punct, read_name, tok_at


def check_batch(b, e, ctx):
    toks = b.toks
    n = len(toks)
    sec, st, rel = e.section, e.stype, e.rel
    heads = find_routine_headers(toks)

    # batch structure
    if heads:
        h = heads[0]
        pre = [t for t in toks[:h[0]] if not punct(t, ";")]
        if pre:
            ctx.add("cmp.batch_first", "CRITICAL", sec, st, rel, [pre[0].line, toks[h[0]].line],
                    "%s %s at L%d is not the first statement in its batch (batch starts with '%s' at L%d)."
                    % (h[3], h[2], toks[h[0]].line, pre[0].text, pre[0].line))
        for h2 in heads[1:]:
            ctx.add("cmp.batch_first", "CRITICAL", sec, st, rel, [toks[h[0]].line, toks[h2[0]].line],
                    "%s %s at L%d is in the same batch as the %s at L%d; GO is missing between them."
                    % (h2[3], h2[2], toks[h2[0]].line, h[2], toks[h[0]].line))

    # duplicate variable declarations (incl. routine parameters)
    declared = {}

    def decl(t):
        k = t.text.lower()
        if k in declared:
            ctx.add("cmp.dup_declare", "CRITICAL", sec, st, rel, [declared[k], t.line],
                    "Variable %s is declared more than once in this batch (L%d and L%d)." % (t.text, declared[k], t.line))
        else:
            declared[k] = t.line

    if heads and heads[0][2] in ("PROCEDURE", "FUNCTION"):
        j, depth, prev = heads[0][1], 0, None
        while j < n:
            t = toks[j]
            if punct(t, "("):
                depth += 1
            elif punct(t, ")"):
                depth -= 1
            elif depth == 0 and kw(t, "AS", "RETURNS", "WITH", "BEGIN"):
                if t.up == "RETURNS" and tok_at(toks, j + 1) is not None and toks[j + 1].kind == "V":
                    decl(toks[j + 1])
                break
            elif t.kind == "V" and (prev is None or punct(prev, "(") or punct(prev, ",")):
                decl(t)
            prev = t
            j += 1
    for i, t in enumerate(toks):
        if not kw(t, "DECLARE"):
            continue
        j, depth, expect = i + 1, 0, True
        while j < n:
            u = toks[j]
            if punct(u, "("):
                depth += 1
            elif punct(u, ")"):
                depth -= 1
                if depth < 0:
                    break
            elif depth == 0:
                if punct(u, ","):
                    expect = True
                    j += 1
                    continue
                if punct(u, ";"):
                    break
                if u.kind == "V" and expect:
                    decl(u)
                elif u.kind == "W" and u.up in DECL_STOP:
                    break
                expect = False
            j += 1

    # duplicate CREATE TABLE / SELECT INTO #temp
    created = {}
    last_kw, depth = {}, 0

    def reg(parts, line):
        key = ".".join(x.lower() for x in parts if x)
        temp = parts[-1].startswith("#")
        if key in created:
            ctx.add("cmp.dup_create", "CRITICAL" if temp else "WARNING", sec, st, rel, [created[key], line],
                    "%s %s is created more than once in this batch (L%d and L%d)."
                    % ("Temp table" if temp else "Table", ".".join(parts), created[key], line))
        else:
            created[key] = line

    for i, t in enumerate(toks):
        if punct(t, "("):
            depth += 1
            last_kw.pop(depth, None)
            continue
        if punct(t, ")"):
            last_kw.pop(depth, None)
            depth = max(0, depth - 1)
            continue
        if kw(t, "SELECT", "OUTPUT", "INSERT", "MERGE", "FETCH", "UPDATE", "DELETE"):
            last_kw[depth] = t.up
        if kw(t, "CREATE") and kw(tok_at(toks, i + 1), "TABLE"):
            parts, _ = read_name(toks, i + 2)
            if parts:
                reg(parts, t.line)
        elif kw(t, "INTO") and tok_at(toks, i + 1) is not None and toks[i + 1].kind == "T" \
                and last_kw.get(depth) == "SELECT":
            reg([toks[i + 1].text], toks[i + 1].line)

    # BEGIN/END pairing
    stack = []
    for i, t in enumerate(toks):
        if t.kind != "W" or punct(tok_at(toks, i - 1), "."):
            continue
        nx = tok_at(toks, i + 1)
        if t.up == "BEGIN":
            if kw(nx, "TRAN", "TRANSACTION", "DISTRIBUTED", "CONVERSATION", "DIALOG"):
                continue
            stack.append(("TRY" if kw(nx, "TRY") else "CATCH" if kw(nx, "CATCH") else "BEGIN", t.line))
        elif t.up == "CASE":
            stack.append(("CASE", t.line))
        elif t.up == "END":
            if kw(nx, "CONVERSATION"):
                continue
            want = nx.up if kw(nx, "TRY", "CATCH") else None
            if not stack:
                ctx.add("cmp.begin_end", "CRITICAL", sec, st, rel, [t.line],
                        "END at L%d has no matching BEGIN/CASE in this batch." % t.line)
                continue
            kind, ln = stack.pop()
            if want and kind != want:
                ctx.add("cmp.begin_end", "CRITICAL", sec, st, rel, [ln, t.line],
                        "END %s at L%d closes %s opened at L%d." % (want, t.line, kind, ln))
            elif not want and kind in ("TRY", "CATCH"):
                ctx.add("cmp.begin_end", "CRITICAL", sec, st, rel, [ln, t.line],
                        "END at L%d closes BEGIN %s (L%d); expected END %s." % (t.line, kind, ln, kind))
    for kind, ln in stack:
        label = "CASE" if kind == "CASE" else ("BEGIN " + kind if kind in ("TRY", "CATCH") else "BEGIN")
        ctx.add("cmp.begin_end", "CRITICAL", sec, st, rel, [ln, b.end],
                "%s at L%d has no matching END (batch ends at L%d)." % (label, ln, b.end))

    # parentheses
    pst = []
    for t in toks:
        if punct(t, "("):
            pst.append(t.line)
        elif punct(t, ")"):
            if pst:
                pst.pop()
            else:
                ctx.add("cmp.parens", "CRITICAL", sec, st, rel, [t.line], "Unmatched ) at L%d." % t.line)
    for ln in pst:
        ctx.add("cmp.parens", "CRITICAL", sec, st, rel, [ln, b.end],
                "Unmatched ( opened at L%d is never closed (batch ends at L%d)." % (ln, b.end))
