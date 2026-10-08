import pytest

from ace_db_verify.changelog import parse_changelog

pytestmark = pytest.mark.unit


def test_parse_sample_changelog(sample_changelog):
    cl = parse_changelog(sample_changelog)
    assert cl.ok
    assert cl.root_tag == "databaseChangeLog"
    assert [c[0] for c in cl.changesets] == ["R1"]
    assert [s["placement"] for s in cl.sqlfiles] == ["Deploy", "Deploy", "Deploy", "Rollback", "Rollback"]
    assert cl.has_rollback


def test_sqlfile_outside_changeset_is_not_run(tmp_path):
    p = tmp_path / "c.xml"
    p.write_text('<databaseChangeLog><changeSet id="a"><other><sqlFile path="x.sql"/></other></changeSet>'
                 "</databaseChangeLog>", encoding="utf-8")
    assert parse_changelog(str(p)).sqlfiles[0]["placement"] == "NotRun"


def test_malformed_xml_reports_line(tmp_path):
    p = tmp_path / "bad.xml"
    p.write_text("<databaseChangeLog>\n<changeSet>\n</databaseChangeLog>", encoding="utf-8")
    cl = parse_changelog(str(p))
    assert not cl.ok
    assert cl.error[0] == 3
