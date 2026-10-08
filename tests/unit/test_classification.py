import pytest

from ace_db_verify.classification import classify, lines_str

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "rel, expected",
    [
        ("releases/R1/Deploy/Tables/dbo.Customer.Table.sql",
         ("Deploy", "Table", "dbo", "Customer", "dbo.Customer.Table.sql", "Customer.Table.sql",
          "tables/dbo.customer.table.sql")),
        ("releases/R1/Rollback/Procedures/cmy.GetX.StoredProcedure.sql",
         ("Rollback", "Procedure", "cmy", "GetX", "cmy.GetX.StoredProcedure.sql", "GetX.StoredProcedure.sql",
          "procedures/cmy.getx.storedprocedure.sql")),
        ("misc/Functions/dbo.fn_x.sql",
         ("Unknown", "Tvf", "dbo", "fn_x", "dbo.fn_x.sql", "fn_x.sql", "misc/functions/dbo.fn_x.sql")),
        ("loose.sql", ("Unknown", "Other", "", "loose", "loose.sql", "loose.sql", "loose.sql")),
    ],
)
def test_classify(rel, expected):
    assert classify(rel) == expected


def test_lines_str_truncates_after_six():
    assert lines_str([1, 2]) == "L1, L2"
    assert lines_str(list(range(1, 9))).endswith(" …")
