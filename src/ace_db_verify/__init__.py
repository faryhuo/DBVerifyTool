"""
ace_db_verify - ACE DB database scripts verification (static, no Liquibase).

Reads the changelog XML the same way run_changelog_sql.bat / run_rollback_sql.bat do:
  * Deploy   = <sqlFile> elements that are direct children of <changeSet>, in document order
  * Rollback = <sqlFile> elements anywhere inside <rollback>, in document order
  * path     = resolved relative to the changelog file's own directory
and checks every script the way `sqlcmd -b -f 65001 -i <file>` would see it
(GO batch separators, QUOTED_IDENTIFIER OFF, $(var) substitution, UTF-8 input).

Output: one self-contained HTML report (+ JSON). Nothing is executed against a database
unless --compile is given; --compile runs each batch through sqlcmd under SET NOEXEC ON
(compile only, no changes to the database).

Exit code: 0 = OK, 1 = blocking issues found (see --fail-on), 2 = tool error.
Python 3.8+, standard library only.
"""
from .constants import VERSION

__version__ = VERSION
__all__ = ["__version__", "main"]


def main(argv=None):
    from .cli import main as _main
    return _main(argv)
