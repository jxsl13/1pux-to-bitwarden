# Contributing

## Local checks

Use Python 3.10 or newer and run `python3 -m unittest discover -v` on macOS/Linux or `py -3 -m unittest discover -v` on Windows. `make test` is also available; `make check` checks both entry points and the macOS launcher syntax when zsh is available. The tests use Python's standard library, temporary synthetic archives, and a simulated Bitwarden CLI. Windows permission checks use the system's Windows PowerShell.

CI covers Linux and Windows with Python 3.10 and 3.14, plus macOS with Python 3.14. A separate Windows smoke check installs the official npm CLI and invokes only `--version` in a temporary profile. Keep subprocess calls free of command shells, read/write JSON as UTF-8, and preserve native locking and private file permissions on all three operating systems.

Keep documentation, comments, diagnostics, and new report fields in English. Preserve compatibility with existing source identifiers and migration state when changing formats.

## Test data

Use generated, fictional data only. Do not add real vault exports, account identifiers, attachment content, recovery keys, session tokens, or reports to fixtures or issue descriptions.

The repository uses a deny-by-default `.gitignore`. Add an explicit exception when introducing a new project file. Before committing, review `git diff --cached` and `git diff --cached --name-only`.

Changes to import or recovery logic should cover partial failures, lost responses, and duplicate prevention. Tests must not invoke a real authenticated vault.

## Scope

Keep imports explicit and previewable. Do not introduce automatic deletion, destructive rollback, fuzzy attachment matching, or hidden credential storage. Avoid adding dependencies where the standard library is sufficient.
