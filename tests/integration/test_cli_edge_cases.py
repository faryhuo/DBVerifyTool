import json

import pytest

from ace_db_verify.cli import main

pytestmark = pytest.mark.integration

CHANGELOG = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog>
  <changeSet id="R2" author="dev">
    <sqlFile path="../releases/R2/Deploy/Procedures/dbo.UseT.StoredProcedure.sql" dbms="mssql" encoding="UTF-8"/>
    <sqlFile path="../releases/R2/Deploy/Tables/dbo.T.Table.sql" dbms="mssql" encoding="UTF-8"/>
    <sqlFile path="../releases/R2/Deploy/Tables/dbo.Missing.Table.sql" dbms="mssql" encoding="UTF-8"/>
    <sqlFile path="../releases/R2/Deploy/Tables/dbo.T.Table.sql" dbms="oracle"/>
    <other><sqlFile path="../releases/R2/Deploy/Tables/dbo.T.Table.sql"/></other>
    <rollback>
      <sqlFile path="../releases/R2/Rollback/Tables/dbo.T.Table.sql" dbms="mssql" encoding="UTF-8"/>
      <sqlFile path="../releases/R2/Rollback/Procedures/dbo.UseT.StoredProcedure.sql" dbms="mssql" encoding="UTF-8"/>
    </rollback>
  </changeSet>
</databaseChangeLog>
"""

FILES = {
    "Deploy/Procedures/dbo.UseT.StoredProcedure.sql":
        "CREATE PROCEDURE dbo.UseT AS\nBEGIN\n  SELECT Id FROM dbo.T;\n  SELECT 1 FROM csg.Other;\nEND\nGO\n",
    "Deploy/Tables/dbo.T.Table.sql": "CREATE TABLE dbo.T (Id INT);\nGO\nALTER TABLE dbo.T DROP Id;\nGO\n",
    "Rollback/Tables/dbo.T.Table.sql": "ALTER TABLE dbo.T ADD X INT;\nGO\n",
    "Rollback/Procedures/dbo.UseT.StoredProcedure.sql": "USE OtherDb;\nGO\nDROP PROCEDURE dbo.UseT;\nGO\n",
}


@pytest.fixture
def broken_project(tmp_path):
    (tmp_path / "changelogs").mkdir()
    (tmp_path / "changelogs" / "changelog.xml").write_text(CHANGELOG, encoding="utf-8")
    for rel, body in FILES.items():
        p = tmp_path / "releases" / "R2" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def _checks(tmp_path, changelog):
    out = tmp_path / "out" / "r.html"
    rc = main(["--changelog", str(changelog), "--out", str(out), "--quiet", "--compile", "--server", "s", "--database", "d"])
    result = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    return rc, {i["check"] for i in result["issues"]}, result


def test_broken_project_reports_expected_checks(broken_project, monkeypatch):
    monkeypatch.chdir(broken_project)
    monkeypatch.setenv("PATH", "")  # sqlcmd unavailable -> compile step skipped, static mode
    rc, checks, result = _checks(broken_project, broken_project / "changelogs" / "changelog.xml")

    assert rc == 1
    assert result["meta"]["mode"] == "Static (no database)"
    assert result["meta"]["release"] == "R2"
    expected = {"inv.missing", "xml.duplicate", "xml.attributes", "xml.not_run", "dep.order",
                "ver.rollback_logic", "ver.schema_mismatch", "cmp.alter_drop", "cmp.use_db"}
    missing = expected - checks
    assert not missing, "not reported: %s (got %s)" % (missing, sorted(checks))


def test_malformed_changelog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.xml"
    bad.write_text("<databaseChangeLog>\n<changeSet>\n", encoding="utf-8")
    rc, checks, _ = _checks(tmp_path, bad)
    assert rc == 1
    assert "xml.malformed" in checks
