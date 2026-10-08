"""Extract CREATE / ALTER / DROP operations from token streams."""
from __future__ import annotations

from ..lexer import OBJ_KINDS, STMT_START, kw, punct, read_name, tok_at
from ..models import Op


def extract_ops(toks):
    ops, n, i = [], len(toks), 0
    while i < n:
        t = toks[i]
        if kw(t, "CREATE", "ALTER", "DROP") and not punct(tok_at(toks, i - 1), "."):
            j, op = i + 1, t.up
            if op == "CREATE" and kw(tok_at(toks, j), "OR") and kw(tok_at(toks, j + 1), "ALTER"):
                op, j = "CREATE OR ALTER", j + 2
            if j < n and kw(toks[j], *OBJ_KINDS):
                kind = OBJ_KINDS[toks[j].up]
                j += 1
                if op == "DROP" and kw(tok_at(toks, j), "IF") and kw(tok_at(toks, j + 1), "EXISTS"):
                    j += 2
                while True:
                    parts, e = read_name(toks, j)
                    if not parts:
                        break
                    ops.append(Op(op, kind, parts, t.line, e))
                    j = e
                    if op == "DROP" and punct(tok_at(toks, j), ","):
                        j += 1
                        continue
                    break
                i = max(j, i + 1)
                continue
        i += 1
    return ops


CONSTRAINT_WORDS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "DEFAULT", "INDEX", "PERIOD"}


def parse_add_columns(toks, j):
    cols, depth, expect, n = [], 0, True, len(toks)
    while j < n:
        t = toks[j]
        if punct(t, "("):
            depth += 1
        elif punct(t, ")"):
            depth -= 1
            if depth < 0:
                break
        elif depth == 0:
            if punct(t, ","):
                expect = True
                j += 1
                continue
            if punct(t, ";"):
                break
            if expect:
                if t.kind in ("W", "I", "Q") and not (t.kind == "W" and t.up in CONSTRAINT_WORDS):
                    cols.append(t.val)
                expect = False
            elif t.kind == "W" and t.up in STMT_START:
                break
        j += 1
    return cols


def parse_drop_columns(toks, j):
    cols, n = [], len(toks)
    if kw(tok_at(toks, j), "IF") and kw(tok_at(toks, j + 1), "EXISTS"):
        j += 2
    while j < n:
        t = toks[j]
        if t.kind in ("W", "I", "Q") and not kw(t, "COLUMN", "CONSTRAINT"):
            cols.append(t.val)
            j += 1
        else:
            break
        if punct(tok_at(toks, j), ","):
            j += 1
            if kw(tok_at(toks, j), "COLUMN"):
                j += 1
            if kw(tok_at(toks, j), "CONSTRAINT"):
                break
            continue
        break
    return cols


def main_op(ops, obj, kinds):
    seq = [o for o in ops if o.kind in kinds and o.parts and o.parts[-1].lower() == obj.lower()]
    if not seq:
        return "NONE", ""
    names = [o.op for o in seq]
    kind = seq[0].kind
    if names[-1] == "DROP":
        return "DROP", kind     # final state: object removed (e.g. DROP COLUMN ... then DROP TABLE)
    if "DROP" in names:
        di = names.index("DROP")
        if any(x.startswith("CREATE") for x in names[di + 1:]):
            return "RECREATE", kind
    for cand in ("CREATE OR ALTER", "CREATE", "ALTER", "DROP"):
        if cand in names:
            return cand, kind
    return "NONE", kind
