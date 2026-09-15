# Security

This utility handles plaintext password-manager exports and temporary attachment files. Use a private working directory and retain migration state until the migration is complete.

## Reporting a vulnerability

Use the repository's private vulnerability reporting feature if enabled, or contact the maintainer privately. Do not include real exports, passwords, tokens, recovery codes, or attachment contents in a public issue. A minimal synthetic example and the affected version are enough to begin an investigation.

## Repository boundaries

Only the explicitly approved source, documentation, and configuration files are tracked. The deny-by-default `.gitignore` excludes migration output and unexpected files. Ignoring a file does not encrypt it, and `git add -f` bypasses these rules.

Tests use synthetic data and a fake CLI. CI has read-only repository permissions, uses actions pinned to commit IDs, and does not require vault credentials or upload migration artifacts.
