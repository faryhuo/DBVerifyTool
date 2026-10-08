"""Read SQL files and split them into GO batches."""
from __future__ import annotations

import codecs
from collections import defaultdict

from ..lexer import GO_LINE, ROUTINE_KINDS, kw, punct, read_name, tok_at
from ..models import Batch


def read_sql(path, encoding, ctx, e, report=True):
    with open(path, "rb") as f:
        raw = f.read()
    sec, st, rel = (e.section, e.stype, e.rel) if e else ("", "", "")
    add = ctx.add if report else (lambda *a, **k: None)
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        add("cmp.encoding", "CRITICAL", sec, st, rel, [1],
            "File is UTF-16 (SSMS 'Unicode'); the bat reads input as UTF-8 (-f 65001).")
        return raw.decode("utf-16"), False
    if b"\x00" in raw[:4000]:
        add("cmp.encoding", "CRITICAL", sec, st, rel, [1],
            "File contains NUL bytes (looks like UTF-16 without BOM); sqlcmd -f 65001 cannot read it.")
        return raw.decode("utf-16-le", errors="replace"), False
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[3:]
    enc = encoding or "utf-8"
    try:
        return raw.decode(enc), True
    except LookupError:
        return raw.decode("utf-8", errors="replace"), True
    except UnicodeDecodeError as ex:
        line = raw[:ex.start].count(b"\n") + 1
        add("cmp.encoding", "WARNING", sec, st, rel, [line],
            "Bytes at L%d are not valid %s (probably ANSI/cp1252); non-ASCII text will be garbled." % (line, enc))
        return raw.decode(enc, errors="replace"), True


def split_batches(fa, ctx, e):
    toks, lines = fa.toks, fa.lines
    by_line = defaultdict(list)
    for idx, t in enumerate(toks):
        by_line[t.line].append(idx)
    sep = set()
    sep_lines = set()
    for ln in sorted(by_line):
        idxs = by_line[ln]
        t0 = toks[idxs[0]]
        if kw(t0, "GO"):
            rest = [toks[k] for k in idxs[1:]]
            if not rest or (len(rest) == 1 and rest[0].kind == "N"):
                sep.update(idxs)
                sep_lines.add(ln)
                if not GO_LINE.match(lines[ln - 1]):
                    ctx.add("cmp.go_format", "WARNING", e.section, e.stype, e.rel, [ln],
                            "GO at L%d shares its line with other text; sqlcmd may not treat it as a separator." % ln)
            else:
                ctx.add("cmp.go_format", "CRITICAL", e.section, e.stype, e.rel, [ln],
                        "'GO %s' at L%d is not a valid batch separator; sqlcmd fails to parse it." % (rest[0].text, ln))
        else:
            last = toks[idxs[-1]]
            if kw(last, "GO") and len(idxs) > 1:
                ctx.add("cmp.go_format", "CRITICAL", e.section, e.stype, e.rel, [ln],
                        "GO at the end of L%d is not on its own line, so it does not separate batches." % ln)
    # GO lines hidden inside block comments / multi-line strings
    for ln, text in enumerate(lines, 1):
        if ln in sep_lines or not GO_LINE.match(text):
            continue
        for kind, s, en in fa.spans:
            if s < ln <= en:
                if kind == "C":
                    ctx.add("cmp.go_format", "CRITICAL", e.section, e.stype, e.rel, [s, ln],
                            "GO at L%d is inside a /* */ comment opened at L%d; sqlcmd still splits the batch there "
                            "(Msg 113 'Missing end comment mark')." % (ln, s))
                else:
                    ctx.add("cmp.go_format", "WARNING", e.section, e.stype, e.rel, [s, ln],
                            "GO at L%d is inside a multi-line string starting at L%d; sqlcmd may split the batch there." % (ln, s))
                break
    batches, cur, start = [], [], 1
    for idx, t in enumerate(toks):
        if idx in sep:
            if kw(t, "GO"):
                batches.append(Batch(start, t.line - 1, cur))
                cur, start = [], t.line + 1
            continue
        cur.append(t)
    batches.append(Batch(start, max(len(lines), start), cur))
    return batches


def find_routine_headers(toks):
    res = []
    n = len(toks)
    for i, t in enumerate(toks):
        if kw(t, "CREATE", "ALTER") and not punct(tok_at(toks, i - 1), ".") \
                and not (t.up == "ALTER" and kw(tok_at(toks, i - 1), "OR")):
            j, op = i + 1, t.up
            if t.up == "CREATE" and kw(tok_at(toks, j), "OR") and kw(tok_at(toks, j + 1), "ALTER"):
                j, op = j + 2, "CREATE OR ALTER"
            if j < n and kw(toks[j], *ROUTINE_KINDS):
                kind = "PROCEDURE" if toks[j].up == "PROC" else toks[j].up
                parts, e = read_name(toks, j + 1)
                res.append((i, e, kind, op, parts or []))
    return res
