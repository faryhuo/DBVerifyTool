"""T-SQL tokenizer and token helpers."""
from __future__ import annotations

import re


class Tok:
    __slots__ = ("kind", "text", "line", "up", "val")

    def __init__(self, kind, text, line, val=None):
        self.kind = kind      # W word, I [ident], Q "ident", S 'string', V @var, T #temp, N number, P punct
        self.text = text
        self.line = line
        self.val = text if val is None else val
        self.up = self.val.upper() if kind == "W" else ""


_WS = re.compile("[ \t\r\f\v\u00a0\u200b\u200c\u200d\u2060\ufeff\u3000]+")
_WORD = re.compile(r"[^\W\d][\w@$#]*")
_NUM = re.compile(r"0[xX][0-9A-Fa-f]*|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
_VAR = re.compile(r"@@?[\w@$#]*")
_TEMP = re.compile(r"##?[\w@$#]+")


def _scan_quoted(text, i, close):
    """Scan a literal starting at text[i] (opening char) with doubled-close escaping."""
    n = len(text)
    j = i + 1
    while j < n:
        k = text.find(close, j)
        if k < 0:
            return n, False
        if k + 1 < n and text[k + 1] == close:
            j = k + 2
            continue
        return k + 1, True
    return n, False


def lex(text):
    toks, spans, errors = [], [], []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        m = _WS.match(text, i)
        if m:
            i = m.end()
            continue
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and text.startswith("/*", i):
            start, depth, j = line, 0, i
            while j < n:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        break
                else:
                    if text[j] == "\n":
                        line += 1
                    j += 1
            if depth > 0:
                errors.append((start, "Unterminated /* comment */ starting at L%d." % start))
            spans.append(("C", start, line))
            i = j
            continue
        if c == "'" or (c in "Nn" and i + 1 < n and text[i + 1] == "'"):
            q = i if c == "'" else i + 1
            j, closed = _scan_quoted(text, q, "'")
            s = text[i:j]
            start = line
            if not closed:
                errors.append((start, "Unterminated string literal starting at L%d." % start))
            toks.append(Tok("S", s, start))
            line += s.count("\n")
            spans.append(("S", start, line))
            i = j
            continue
        if c == "[" or c == '"':
            close = "]" if c == "[" else '"'
            j, closed = _scan_quoted(text, i, close)
            s = text[i:j]
            if not closed:
                errors.append((line, "Unterminated %s identifier starting at L%d." % ("[...]" if c == "[" else '"..."', line)))
                inner = s[1:]
            else:
                inner = s[1:-1].replace(close * 2, close)
            toks.append(Tok("I" if c == "[" else "Q", s, line, inner))
            line += s.count("\n")
            i = j
            continue
        if c == "@":
            m = _VAR.match(text, i)
            toks.append(Tok("V", m.group(), line))
            i = m.end()
            continue
        if c == "#":
            m = _TEMP.match(text, i)
            if m:
                toks.append(Tok("T", m.group(), line))
                i = m.end()
                continue
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            m = _NUM.match(text, i)
            toks.append(Tok("N", m.group(), line))
            i = m.end()
            continue
        m = _WORD.match(text, i)
        if m:
            toks.append(Tok("W", m.group(), line))
            i = m.end()
            continue
        toks.append(Tok("P", c, line))
        i += 1
    return toks, spans, errors


def kw(t, *words):
    return t is not None and t.kind == "W" and t.up in words


def punct(t, ch):
    return t is not None and t.kind == "P" and t.text == ch


def tok_at(toks, i):
    return toks[i] if 0 <= i < len(toks) else None


def read_name(toks, i):
    """Read a dotted multipart name starting at i. Returns (parts, next_index)."""
    t = tok_at(toks, i)
    if t is None or t.kind not in ("W", "I", "Q", "T"):
        return None, i
    parts, j = [t.val], i + 1
    while punct(tok_at(toks, j), "."):
        nx = tok_at(toks, j + 1)
        if nx is not None and nx.kind in ("W", "I", "Q"):
            parts.append(nx.val)
            j += 2
        elif punct(nx, "."):
            parts.append("")
            j += 1
        else:
            break
    return parts, j


STMT_START = {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "IF", "ELSE", "BEGIN", "END", "DECLARE", "SET",
              "EXEC", "EXECUTE", "WHILE", "RETURN", "PRINT", "RAISERROR", "THROW", "CREATE", "ALTER", "DROP",
              "TRUNCATE", "OPEN", "FETCH", "CLOSE", "DEALLOCATE", "GOTO", "BREAK", "CONTINUE", "COMMIT",
              "ROLLBACK", "SAVE", "USE", "GRANT", "REVOKE", "DENY", "WAITFOR", "GO"}
DECL_STOP = STMT_START - {"END", "ELSE"}
OBJ_KINDS = {"PROCEDURE": "PROCEDURE", "PROC": "PROCEDURE", "FUNCTION": "FUNCTION", "TABLE": "TABLE",
             "VIEW": "VIEW", "TRIGGER": "TRIGGER", "SYNONYM": "SYNONYM", "TYPE": "TYPE"}
ROUTINE_KINDS = ("PROCEDURE", "PROC", "FUNCTION", "VIEW", "TRIGGER")
TYPE_KINDS = {"Tvf": ("FUNCTION",), "Procedure": ("PROCEDURE",), "Table": ("TABLE",)}
GO_LINE = re.compile(r"^\s*go(?:\s+\d+)?\s*(?:--.*)?$", re.I)
HIDDEN = {"\u00a0": "NBSP", "\u200b": "zero-width space", "\u200c": "ZWNJ", "\u200d": "ZWJ", "\u2060": "word joiner",
          "\ufeff": "BOM/ZWNBSP", "\u2028": "line separator", "\u2029": "paragraph separator", "\u3000": "ideographic space"}
