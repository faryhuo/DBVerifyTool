import pytest

from ace_db_verify.analysis.operations import extract_ops, main_op
from ace_db_verify.lexer import lex

pytestmark = pytest.mark.unit

TABLE = ("TABLE",)
PROC = ("PROCEDURE",)


def _ops(sql):
    return extract_ops(lex(sql)[0])


def test_extract_create_or_alter_procedure():
    ops = _ops("CREATE OR ALTER PROCEDURE dbo.GetX AS SELECT 1")
    assert [(o.op, o.kind, o.parts) for o in ops] == [("CREATE OR ALTER", "PROCEDURE", ["dbo", "GetX"])]


def test_extract_drop_if_exists():
    ops = _ops("DROP TABLE IF EXISTS dbo.A")
    assert [(o.op, o.parts) for o in ops] == [("DROP", ["dbo", "A"])]


@pytest.mark.parametrize(
    "sql, obj, kinds, expected",
    [
        ("CREATE TABLE dbo.A (Id INT)", "A", TABLE, ("CREATE", "TABLE")),
        ("ALTER TABLE dbo.A ADD B INT", "A", TABLE, ("ALTER", "TABLE")),
        ("DROP TABLE dbo.A", "A", TABLE, ("DROP", "TABLE")),
        ("DROP TABLE dbo.A\nGO\nCREATE TABLE dbo.A (Id INT)", "A", TABLE, ("RECREATE", "TABLE")),
        ("CREATE PROCEDURE dbo.P AS SELECT 1", "Other", PROC, ("NONE", "")),
    ],
)
def test_main_op(sql, obj, kinds, expected):
    assert main_op(_ops(sql), obj, kinds) == expected
