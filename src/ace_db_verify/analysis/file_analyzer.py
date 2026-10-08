"""Analyze a single SQL file."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .batch_checks import check_batch
from .operations import extract_ops, main_op, parse_add_columns, parse_drop_columns
from .reader import read_sql, split_batches
from ..classification import lines_str
from ..lexer import HIDDEN, TYPE_KINDS, kw, lex, punct, read_name, tok_at
from ..models import Ctx, FileAnalysis


def analyze_file(e, cfg, ctx, checks=True):
    fa = FileAnalysis()
    sink = ctx if checks else Ctx(cfg)
    text, fa.readable = read_sql(e.abspath, e.attrs.get("encoding") or cfg["expected_encoding"], sink, e)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fa.text = text
    fa.lines = text.split("\n")
    fa.toks, fa.spans, lex_errors = lex(text)
    fa.preexisting_marker = bool(re.search(r"verify\s*:\s*preexisting", text, re.I))
    sec, st, rel = e.section, e.stype, e.rel
    for ln, msg in lex_errors:
        sink.add("cmp.unterminated", "CRITICAL", sec, st, rel, [ln], msg)

    # hidden characters
    hidden = defaultdict(list)
    for ln, line in enumerate(fa.lines, 1):
        for ch, name in HIDDEN.items():
            if ch in line and not (ln == 1 and ch == "\ufeff" and line.startswith(ch)):
                hidden[name].append(ln)
    for name, lns in hidden.items():
        sink.add("cmp.hidden_chars", "WARNING", sec, st, rel, lns[:10],
                 "%s found at %s." % (name, lines_str(lns)))

    # sqlcmd scripting-variable and command lines
    var_lines = [ln for ln, line in enumerate(fa.lines, 1) if re.search(r"\$\([A-Za-z_][\w]*\)", line)]
    if var_lines:
        sink.add("cmp.sqlcmd_var", "CRITICAL", sec, st, rel, var_lines[:10],
                 "$(...) at %s is a sqlcmd scripting variable; sqlcmd stops with 'scripting variable not defined'."
                 % lines_str(var_lines))
    cmd_lines = [ln for ln, line in enumerate(fa.lines, 1)
                 if re.match(r"^\s*(:[A-Za-z]|!!|(exit|quit)\b)", line, re.I)]
    if cmd_lines:
        fa.compile_unsafe = "contains sqlcmd command lines (%s)" % lines_str(cmd_lines)
    if any(kw(t, "NOEXEC") for t in fa.toks):
        fa.compile_unsafe = "script toggles SET NOEXEC"

    fa.batches = split_batches(fa, sink, e)
    if checks:
        for b in fa.batches:
            check_batch(b, e, ctx)

    toks = fa.toks
    fa.ops = extract_ops(toks)

    # ALTER TABLE actions (added / dropped columns, DROP without COLUMN)
    for o in fa.ops:
        if o.op != "ALTER" or o.kind != "TABLE":
            continue
        j = o.end
        if kw(tok_at(toks, j), "WITH") and kw(tok_at(toks, j + 1), "CHECK", "NOCHECK"):
            j += 2
        a = tok_at(toks, j)
        key = o.parts[-1].lower()
        if kw(a, "ADD"):
            fa.added_cols[key] += parse_add_columns(toks, j + 1)
        elif kw(a, "DROP"):
            nx = tok_at(toks, j + 1)
            if kw(nx, "COLUMN"):
                fa.dropped_cols[key] += parse_drop_columns(toks, j + 2)
            elif kw(nx, "CONSTRAINT", "PERIOD", "INDEX"):
                pass
            elif nx is not None:
                sev = "WARNING" if kw(nx, "IF") else "CRITICAL"
                nm = tok_at(toks, j + 3).val if kw(nx, "IF") and tok_at(toks, j + 3) else nx.val
                sink.add("cmp.alter_drop", sev, sec, st, rel, [a.line],
                         "ALTER TABLE %s DROP %s at L%d drops a CONSTRAINT named '%s', not a column; "
                         "write DROP COLUMN." % (".".join(o.parts), nm, a.line, nm))

    kinds = TYPE_KINDS.get(e.stype)
    if kinds:
        fa.main_op, fa.main_kind = main_op(fa.ops, e.obj, kinds)
        if fa.main_op == "NONE":
            fa.op_display = "—"
        elif fa.main_op == "RECREATE":
            fa.op_display = "DROP + CREATE " + fa.main_kind
        else:
            fa.op_display = fa.main_op + " " + fa.main_kind
        if e.stype == "Table" and fa.main_op == "ALTER":
            k = e.obj.lower()
            bits = []
            if fa.added_cols.get(k):
                bits.append("ADD " + ", ".join(fa.added_cols[k]))
            if fa.dropped_cols.get(k):
                bits.append("DROP COLUMN " + ", ".join(fa.dropped_cols[k]))
            if bits:
                fa.op_display += " (" + "; ".join(bits) + ")"
    else:
        seen = []
        for i, t in enumerate(toks):
            if kw(t, "INSERT", "UPDATE", "DELETE", "MERGE") or (kw(t, "TRUNCATE") and kw(tok_at(toks, i + 1), "TABLE")):
                if t.up not in seen:
                    seen.append(t.up)
        fa.dml = seen
        fa.op_display = ", ".join(seen) if seen else "—"

    # references to multipart / single names
    refs, n, i = [], len(toks), 0
    while i < n:
        t = toks[i]
        if t.kind in ("W", "I", "Q") and not punct(tok_at(toks, i - 1), "."):
            parts, j = read_name(toks, i)
            schema = parts[-2] if len(parts) >= 2 else None
            refs.append((schema, parts[-1], t.line))
            i = j
            continue
        i += 1
    fa.refs = refs

    if checks:
        # QUOTED_IDENTIFIER OFF under sqlcmd
        qi_on_line = None
        for i, t in enumerate(toks):
            if kw(t, "QUOTED_IDENTIFIER") and kw(tok_at(toks, i - 1), "SET") and kw(tok_at(toks, i + 1), "ON"):
                qi_on_line = t.line
                break
        q_lines = [t.line for t in toks if t.kind == "Q" and (qi_on_line is None or t.line < qi_on_line)]
        if q_lines:
            ctx.add("cmp.quoted_ident", "WARNING", sec, st, rel, q_lines[:10],
                    "Double-quoted identifier(s) at %s; sqlcmd (no -I) treats them as string literals." % lines_str(q_lines))
        # USE <db>
        target_db = (cfg.get("sqlcmd") or {}).get("database")
        for i, t in enumerate(toks):
            if kw(t, "USE") and (i == 0 or toks[i - 1].line != t.line or punct(toks[i - 1], ";")):
                nx = tok_at(toks, i + 1)
                if nx is not None and nx.kind in ("W", "I", "Q"):
                    if target_db and nx.val.lower() == str(target_db).lower():
                        continue
                    ctx.add("cmp.use_db", "WARNING", sec, st, rel, [t.line],
                            "USE %s at L%d switches database (bat connects to %s)." % (nx.val, t.line, target_db or "-d"))
    return fa


def normalized_hash(fa, schemas):
    pat = re.compile(r"\[?\b(?:%s)\b\]?\s*\." % "|".join(re.escape(s) for s in schemas), re.I)
    out, toks = [], fa.toks
    for i, t in enumerate(toks):
        if t.kind in ("W", "I", "Q") and t.val.lower() in schemas and punct(tok_at(toks, i + 1), "."):
            out.append("<SCHEMA>")
        elif t.kind == "S":
            out.append(pat.sub("<SCHEMA>.", t.text))
        elif t.kind in ("W", "I", "Q"):
            out.append(t.val)
        else:
            out.append(t.text)
    return hashlib.sha256("\x1f".join(out).encode("utf-8")).hexdigest()
