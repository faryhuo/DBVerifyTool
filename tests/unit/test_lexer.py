import pytest

from ace_db_verify.lexer import lex

pytestmark = pytest.mark.unit


def _toks(sql):
    return [(t.kind, t.val) for t in lex(sql)[0]]


def test_lex_skips_comments_and_whitespace():
    assert _toks("-- comment\nSELECT /* block */ 1") == [("W", "SELECT"), ("N", "1")]


def test_lex_token_kinds():
    toks = lex("SELECT @v, #tmp, [My Col], 'it''s' FROM dbo.T")[0]
    assert [t.kind for t in toks if t.kind != "P"] == ["W", "V", "T", "I", "S", "W", "W", "W"]
    assert next(t for t in toks if t.kind == "I").val == "My Col"


def test_lex_tracks_line_numbers():
    assert [t.line for t in lex("SELECT\n1\n,\n2")[0]] == [1, 2, 3, 4]


def test_lex_reports_unterminated_string():
    errors = lex("SELECT 'abc")[2]
    assert errors and "Unterminated string" in errors[0][1]


def test_lex_reports_unterminated_block_comment():
    errors = lex("SELECT 1 /* open")[2]
    assert errors and "comment" in errors[0][1]
