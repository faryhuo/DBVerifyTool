"""Static metadata: version, check catalogue, phases and display labels."""
from __future__ import annotations

from collections import OrderedDict


VERSION = "1.0.0"
WORKFLOW = "ace-db-database-scripts-verification"

# check id -> (phase, title, issue text, recommended fix)
CHECKS = {
    "xml.malformed": ("P7", "XML well-formedness", "Changelog XML is not well-formed.",
                      "Fix the XML syntax error at the reported line."),
    "xml.structure": ("P7", "XML structure", "Changelog root / changeSet declaration problem.",
                      "Fix the changelog structure."),
    "xml.duplicate": ("P7", "Duplicate XML entry", "Duplicate changeSet id or duplicate sqlFile path.",
                      "Remove or rename the duplicate entry; the bat would run the file twice."),
    "xml.attributes": ("P7", "sqlFile attributes", "sqlFile attribute is missing or unexpected.",
                       "Set dbms / encoding consistently on every sqlFile."),
    "xml.placement": ("P7", "Deploy/Rollback placement",
                      "Script placement in the XML does not match its Deploy/Rollback folder.",
                      "Deploy scripts go directly under <changeSet>; rollback scripts go inside <rollback>."),
    "xml.not_run": ("P7", "sqlFile not executed",
                    "sqlFile is neither a direct child of <changeSet> nor inside <rollback>; neither bat runs it.",
                    "Move the entry under <changeSet> or into <rollback>."),
    "xml.order": ("P7", "XML execution order", "sqlFile appears out of XML category order.",
                  "Move the entry into its category block in the changelog."),
    "xml.include": ("P7", "Included changelog", "Included changelog file does not exist.",
                    "Fix the include path."),
    "inv.missing": ("P2", "Missing file", "File referenced in the changelog does not exist on disk.",
                    "Add the file or fix the sqlFile path (paths resolve relative to the changelog folder)."),
    "inv.extra": ("P2", "Extra file", "SQL file exists on disk but is not referenced in the changelog.",
                  "Add a sqlFile entry for it or remove it from the release folder."),
    "inv.entity_set": ("P2", "Entity set incomplete", "Expected schema variant(s) missing for this entity set.",
                       "Add the missing schema scripts, or adjust expected_schemas / entity_set_exclude if the variant is intentionally absent."),
    "inv.content": ("P2", "Cross-schema content mismatch",
                    "Schema variants differ after normalization (comments, whitespace and schema prefixes ignored).",
                    "Align the variants so they differ only by schema prefix."),
    "ver.pairing": ("P3", "Deploy/rollback pairing", "Script has no counterpart in the other section.",
                    "Add the matching Deploy/Rollback script and changelog entry."),
    "ver.rollback_logic": ("P3", "Rollback logic mismatch", "Rollback operation does not reverse the deploy operation.",
                           "Update rollback to match the deploy (CREATE → DROP, ALTER → restore previous definition). "
                           "If the object already exists in PROD, add it to preexisting_objects or put '-- verify:preexisting' in the deploy script."),
    "ver.op_unknown": ("P3", "Operation not detected",
                       "No CREATE/ALTER/DROP for the file's object was found in the script.",
                       "Check that the file name matches the object the script defines."),
    "ver.schema_mismatch": ("P3", "Schema mismatch", "Object reference uses a different schema than the file's schema.",
                            "Change the reference to the file's schema, rename the file, or allowlist the pair in cross_schema_allow."),
    "cmp.encoding": ("P3A", "File encoding", "File is not plain UTF-8 text as read by sqlcmd -f 65001.",
                     "Save the file as UTF-8 (SSMS: Save With Encoding → UTF-8)."),
    "cmp.hidden_chars": ("P3A", "Hidden characters", "Non-breaking / zero-width characters found.",
                         "Replace them with normal spaces."),
    "cmp.unterminated": ("P3A", "Simulated execution", "Unterminated string, quoted identifier or comment.",
                         "Close the literal / comment."),
    "cmp.parens": ("P3A", "Simulated execution", "Unmatched ( or ) would prevent the batch from executing.",
                   "Correct the SQL so the batch parses."),
    "cmp.begin_end": ("P3A", "BEGIN/END pairing", "BEGIN/END, BEGIN TRY/CATCH or CASE/END blocks are not balanced in the batch.",
                      "Close every block inside its own batch (a GO inside a block splits it)."),
    "cmp.dup_declare": ("P3A", "Duplicate variable declaration", "Variable is declared more than once in this batch.",
                        "Remove the duplicate DECLARE or separate the batches with GO."),
    "cmp.dup_create": ("P3A", "Duplicate CREATE TABLE / temp table", "Same table or temp table is created more than once in this batch.",
                       "Use different names or separate the batches with GO."),
    "cmp.batch_first": ("P3A", "Batch structure", "CREATE/ALTER PROCEDURE/FUNCTION/VIEW/TRIGGER must be the first statement in its batch.",
                        "Add GO before the CREATE/ALTER statement."),
    "cmp.go_format": ("P3A", "GO separator", "GO line is not handled the way the script expects.",
                      "Put GO alone on its own line, outside comments and strings."),
    "cmp.alter_drop": ("P3A", "ALTER TABLE ... DROP", "'ALTER TABLE ... DROP <name>' without COLUMN is parsed as DROP CONSTRAINT.",
                       "Write ALTER TABLE ... DROP COLUMN <name>."),
    "cmp.sqlcmd_var": ("P3A", "sqlcmd variable", "$(name) is substituted by sqlcmd; an undefined variable aborts the run.",
                       "Remove the $( ) text or escape it; the bat calls sqlcmd without -x."),
    "cmp.quoted_ident": ("P3A", "QUOTED_IDENTIFIER", "sqlcmd runs with QUOTED_IDENTIFIER OFF (no -I), so \"x\" is a string literal, not an identifier.",
                         "Use [brackets] instead of double quotes, or add SET QUOTED_IDENTIFIER ON before the statement."),
    "cmp.use_db": ("P3A", "USE statement", "Script switches database; the bat already connects with -d.",
                   "Remove the USE statement or make sure it targets the deployment database."),
    "cmp.sqlcmd": ("P3A", "sqlcmd compile (NOEXEC)", "SQL Server reported an error while compiling the batch.",
                   "Fix the reported error; this is what sqlcmd -b would fail on."),
    "dep.order": ("P45", "Deploy dependency order", "Script references an in-release object that is deployed later.",
                  "Move the referenced object's script earlier in the changelog."),
    "rb.order": ("P46", "Rollback dependency order", "A dependency is removed before the scripts that depend on it are rolled back.",
                 "Roll back dependents first (reverse of the deploy order)."),
}

PHASES = OrderedDict([
    ("P2", ("Phase 2 – File Inventory", "XML vs disk counts, entity sets, cross-schema content", "inventory")),
    ("P3", ("Phase 3 – Script Verification", "Script pairing, rollback logic, schema references", "verification")),
    ("P3A", ("Phase 3A – SQL Compilation", "Static compilation and simulated execution", "compilation")),
    ("P45", ("Phase 4 & 5 – Deploy Sequence", "Dependency ordering of deploy scripts", "deploy")),
    ("P46", ("Phase 4.1 & 6 – Rollback Sequence", "Dependency ordering of rollback scripts", "rollback")),
    ("P7", ("Phase 7 – XML Integrity", "Changelog structure and sqlFile attributes", "xml")),
])

SEV_RANK = {"CRITICAL": 0, "WARNING": 1, "INFO": 2, "OK": 3}
TYPE_LABEL = {"Table": "Table", "Tvf": "TVF", "Procedure": "Procedure", "Data": "Data", "Other": "Other"}
TYPE_FOLDER_LABEL = {"Table": "Tables", "Tvf": "TVFs", "Procedure": "Procedures", "Data": "Data", "Other": "Other"}
