# Security

This utility handles plaintext password-manager exports and temporary attachment files. Use a private working directory and retain migration state until the migration is complete.

## Reporting a vulnerability

Use the repository's private vulnerability reporting feature if enabled, or contact the maintainer privately. Do not include real exports, passwords, tokens, recovery codes, or attachment contents in a public issue. A minimal synthetic example and the affected version are enough to begin an investigation.

## Repository boundaries

Only the explicitly approved source, documentation, and configuration files are tracked. The deny-by-default `.gitignore` excludes migration output and unexpected files. Ignoring a file does not encrypt it, and `git add -f` bypasses these rules.

The work directory uses POSIX mode `0700` on macOS/Linux. On Windows, a protected ACL grants the current user access and propagates to newly created files and directories. Windows PowerShell applies this ACL before vault data is written; failure stops the run. Use a dedicated local NTFS directory on Windows. These permissions do not encrypt plaintext data or prevent access by an administrator.

On Windows, official npm CLI wrappers are resolved to the package's Node entry point and executed directly. Neither archive paths nor session tokens are interpolated into command-shell strings. The interactive Python launcher keeps an unlocked session in process memory and restores its prior environment on exit.

Tests use synthetic data and a fake CLI. A Windows smoke check also installs the official CLI and runs `--version` with a temporary profile. CI has read-only repository permissions, uses actions pinned to commit IDs, and does not require vault credentials or upload migration artifacts.
