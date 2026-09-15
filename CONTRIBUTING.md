# Contributing

## Local checks

Use Python 3.10 or newer and run `make test`. On macOS, `make check` also checks the launcher syntax and CLI help. The tests use only Python's standard library, temporary synthetic archives, and a simulated Bitwarden CLI.

Keep documentation, comments, diagnostics, and new report fields in English. Preserve compatibility with existing source identifiers and migration state when changing formats.

## Test data

Use generated, fictional data only. Do not add real vault exports, account identifiers, attachment content, recovery keys, session tokens, or reports to fixtures or issue descriptions.

The repository uses a deny-by-default `.gitignore`. Add an explicit exception when introducing a new project file. Before committing, review `git diff --cached` and `git diff --cached --name-only`.

Changes to import or recovery logic should cover partial failures, lost responses, and duplicate prevention. Tests must not invoke a real authenticated vault.

## Scope

Keep imports explicit and previewable. Do not introduce automatic deletion, destructive rollback, fuzzy attachment matching, or hidden credential storage. Avoid adding dependencies where the standard library is sufficient.
