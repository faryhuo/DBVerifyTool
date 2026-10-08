# ace-db-verify

Static verification of ACE DB deploy / rollback SQL scripts referenced by a Liquibase-style changelog.
It reads the changelog the same way `run_changelog_sql.bat` / `run_rollback_sql.bat` do and checks every
script the way `sqlcmd -b -f 65001 -i <file>` would see it. Output is a self-contained HTML report plus JSON.
Nothing touches a database unless `--compile` is given (then batches are compiled under `SET NOEXEC ON`).

Python 3.8+, standard library only.

## Project layout

```
.
├── ace_db_verify.py          # launcher shim: python ace_db_verify.py ... (no install needed)
├── pyproject.toml
├── src/ace_db_verify/
│   ├── cli.py                # argument parsing and orchestration (entry point)
│   ├── config.py             # DEFAULT_CONFIG, load_config, config file lookup
│   ├── constants.py          # VERSION, check catalogue, phases, labels
│   ├── models.py             # Finding, Entry, FileAnalysis, Ctx ...
│   ├── lexer.py              # T-SQL tokenizer
│   ├── changelog.py          # changelog XML parsing
│   ├── classification.py     # path -> section / type / schema / object
│   ├── analysis/             # per-file analysis (reader, batch checks, operations)
│   ├── phases/               # release-wide phases (inventory, verification, dependencies, XML)
│   ├── compiler.py           # optional sqlcmd NOEXEC compile
│   ├── issues.py             # severity overrides, suppression, aggregation
│   └── report/               # HTML report rendering
├── scripts/                  # kit to copy into the DB repo root
│   ├── run_verify.bat
│   ├── run_changelog_sql.bat
│   ├── run_rollback_sql.bat
│   └── ace_db_verify.config.json
├── tests/                    # pytest: unit/ and integration/ + fixtures/sample_project
└── docs/sample_report.html
```

## Usage

```bash
pip install .                 # installs the `ace-db-verify` command
ace-db-verify --changelog changelogs/changelog_ACE_GWP_ID.xml
python -m ace_db_verify --compile          # also compile with sqlcmd (SET NOEXEC ON)
python ace_db_verify.py --help             # from a checkout, without installing
```

Exit code: `0` OK, `1` blocking issues found (see `--fail-on`), `2` tool error.

`ace_db_verify.config.json` is picked up automatically from the **current directory**; otherwise pass `--config`.

### Using the bat kit in a DB repository

Copy the files in `scripts/` to the DB repo root (next to `changelogs/`). `run_verify.bat` locates the tool in this order:

1. `%ACE_DB_VERIFY_HOME%\ace_db_verify.py` (point it at a checkout of this repo)
2. `ace_db_verify.py` next to the bat
3. the installed package (`python -m ace_db_verify`)

## Development

```bash
pip install -e ".[dev]"
pytest --cov=ace_db_verify --cov-report=term-missing
ruff check src tests
```
