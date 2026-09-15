# 1pux to Bitwarden

Import 1Password entries **and their attachments** into Bitwarden using the official Bitwarden CLI. Includes a preview, exact source-ID mapping, resumable migration state, and attachment verification by download and SHA-256.

An independent migration utility. Not affiliated with 1Password or Bitwarden.

## Requirements

- Python 3.10 or newer. No third-party Python packages are required.
- Bitwarden CLI (`bw`) installed and signed in to the correct server and account.
- macOS, Linux, or Windows. On Windows, use a local NTFS work directory and Windows PowerShell 5.1 or newer (included with supported Windows versions).
- On Windows, either the official `bw.exe` or the npm-installed `@bitwarden/cli` with Node.js on PATH. The utility resolves the official npm wrapper and runs Node directly; arbitrary batch wrappers are not supported.
- A `.1pux` export and permission/storage to attach files to the destination items.

The native import interface was tested with Bitwarden CLI 2026.8.0. Entry conversion is performed by that CLI, so supported fields and item types depend on its version.

## Quick start

Run the interactive launcher from the repository directory:

| Platform | Command |
| --- | --- |
| macOS / Linux | `python3 start.py` |
| Windows (PowerShell or Command Prompt) | `py -3 start.py` |

If Windows has no `py` launcher, use `python start.py` with Python 3.10 or newer on PATH. On macOS you can also double-click **`Start.command`** to select the export with a file picker.

Enter the export path, choose a mode, and unlock the CLI when prompted:

| Mode | Action |
| --- | --- |
| `v` or Enter | Preview the full migration; no entries or attachments are uploaded. |
| `i` | Import entries into **My vault**, then upload and verify attachments. |
| `a` | Add attachments to existing entries only. |

A readable HTML report opens when the run finishes and a desktop is available. Use `--no-browser` for a terminal-only run. The launcher also accepts an export path, `--mode v`, `--mode i`, or `--mode a`, `--bw PATH`, and `--work-dir PATH`.

For an initial full import, My vault must be empty. Items previously imported by this utility are recognized on subsequent runs. Existing organization entries are not changed by full-import mode.

All source vaults in the selected export are imported into My vault. Source vaults are not automatically mapped to destination organizations.

## Command-line usage

Run these commands from the repository directory. If you have not signed in to the CLI, select your server with `bw config server SERVER-URL`, then run `bw login`. The CLI session is separate from the desktop and browser extension sessions.

### macOS / Linux

```sh
bw status
export BW_SESSION="$(bw unlock --raw)"

# Preview the full migration.
python3 import_attachments.py "/path/to/export.1pux" --import-items

# Import entries, then upload and verify attachments.
python3 import_attachments.py "/path/to/export.1pux" --import-items --apply

# Lock the CLI when finished.
bw lock
unset BW_SESSION
```

### Windows PowerShell

```powershell
bw status
$env:BW_SESSION = (& bw unlock --raw)

# Preview the full migration.
py -3 import_attachments.py "C:\Users\you\Downloads\export.1pux" --import-items

# Import entries, then upload and verify attachments.
py -3 import_attachments.py "C:\Users\you\Downloads\export.1pux" --import-items --apply

bw lock
Remove-Item Env:BW_SESSION
```

If PowerShell blocks an npm-installed `bw.ps1`, use `bw.cmd` for the shell commands above, or use the Python launcher. The utility itself does not invoke a command shell or change PowerShell's execution policy.

Never put your master password or session token in a command argument, source file, or report. Use `--bw "/path/to/bw"` or `--bw "C:\path\to\bw.exe"` if the CLI is not on your PATH. The remaining examples use `python3`; on Windows substitute `py -3` and your Windows file path.

## How mapping and verification work

1. Force a complete vault synchronization with `bw sync --force`.
2. Validate attachment references and ZIP checksums before importing entries.
3. Create a temporary copy of the export metadata with an additional custom field, **`1PUX Source ID`**, on each entry. The ID combines the source account, vault, and item identifiers. The original archive is unchanged.
4. Import with `bw import 1password1pux`. Multiple source accounts are processed as separate batches because the tested native importer reads only the first account in each payload.
5. Synchronize again and require exactly one target per expected source ID, including entries without attachments. A successful CLI exit code alone is not sufficient.
6. Upload each attachment to its confirmed target ID, download it again, and compare SHA-256 hashes.

Source IDs remain on the imported entries. Identical titles are therefore unambiguous, and title changes do not break subsequent mapping. The utility verifies entry identity and completeness; it does not prove that every field was converted identically by the native importer.

## Resume an interrupted migration

Keep **`work/base-import-state.json`** until your migration is complete. It records pending import batches and verified target IDs, bound to the source archive, account, and server.

Repeat the same command to resume. Confirmed entries are not imported again, and attachments with matching names and content are reused. A lost CLI response can be recovered when all expected source IDs are found after synchronization.

If a base import was only partially confirmed, the utility stops before uploading attachments. Review the state and make sure the earlier CLI process has finished. Then retry only missing entries:

```sh
python3 import_attachments.py "/path/to/export.1pux" \
  --import-items --apply --retry-missing-items
```

Unknown new entries, duplicate source IDs, or missing previously confirmed items still cause a stop. A source entry rejected by the native importer may need investigation before retrying. The utility does not roll back or delete entries that were already imported.

For a separate migration, select a different private `--work-dir`. Legacy source-ID fields and state files from the original version remain readable; new reports and state files use English names. Existing vault fields are not renamed automatically.

## Attachment-only mode

Omit `--import-items` when the entries already exist:

```sh
python3 import_attachments.py "/path/to/export.1pux"
python3 import_attachments.py "/path/to/export.1pux" --apply
```

Existing source IDs take precedence. Otherwise, exact titles are narrowed using matching usernames, URLs, and notes. Review these inferred matches before uploading. Ambiguous targets are blocked.

For manual mappings, copy `work/mapping-suggested.json` to `mapping.json`. Add or correct entries using the `source_key` and target IDs from the JSON report. An empty mapping value explicitly skips that source entry. Use `--mapping mapping.json`; the launcher reads this file in attachment-only mode.

`--personal-only` limits targets to personal items; `--organization-id UUID` limits them to an organization. Full-import mode cannot be combined with manual mapping or an organization target.

## Reports and status

| File | Purpose |
| --- | --- |
| `work/preview.html` / `work/preview.json` | Migration plan and attachment mapping |
| `work/import-report.html` / `work/import-report.json` | Attachment transfer results |
| `work/base-import-state.json` | Base-import progress and confirmed item IDs |
| `work/mapping-suggested.json` | Suggested manual mappings |

Statuses include `after_base_import`, `ready`, `uploaded_verified`, `existing_verified`, and `blocked`.

Exit codes: `0` = preview/import completed without unresolved issues; `2` = blocked attachments or unrecognized archive files; `1` = error; `130` = interrupted. A successful preview does not mean that anything was uploaded.

## Limitations and local data

- Supports file paths using `ID__filename`, `ID___filename`, or the file ID alone. Explicitly referenced custom icons are excluded from attachment import.
- Same-name attachments with different content are stored separately. Identical content with the same name on the same target is treated as one attachment.
- Unrecognized archive files are reported for review instead of guessed.
- Files larger than 500 MiB are blocked locally; the server and CLI may impose a lower limit or reject uploads for storage, plan, or permission reasons.
- Windows cannot store some attachment filenames, including reserved device names, names with `:`, and names ending in a dot or space. These are blocked before a full import starts, or shown as blocked in attachment-only reports. Use macOS/Linux for that archive, or rename those attachments in 1Password and export again. Filenames are never silently changed.
- Very long paths can exceed Windows or CLI limits. Use a short local `--work-dir`, such as `C:\Users\you\bw-work`, if needed.
- Exports and temporary import metadata contain plaintext secrets. Temporary files use private permissions and are removed on normal completion, handled errors, and interruption. A power failure or forced process termination may leave files behind.
- On macOS/Linux the work directory has mode `0700`; on Windows it receives a protected access-control list granting access only to your current user, inherited by new files. Setup fails before writing vault data if Windows permissions cannot be applied. Choose a dedicated work directory; do not point `--work-dir` at a shared folder or a general-purpose directory.
- Reports include item titles, file names, identifiers, and hashes. Keep the working directory private and outside cloud-synced folders if you do not want these files synchronized.
- `.gitignore` allows only explicitly listed project files. Exports, reports, attachments, migration state, mappings, caches, and unexpected files remain local. Do not override this with `git add -f` for migration data.

## Development

```sh
make test
make check  # Checks both entry points and the macOS launcher if zsh is available.
```

On Windows, run `py -3 -m unittest discover -v`; Make is optional. Tests use synthetic exports and a simulated CLI; they do not need credentials or access a real vault. GitHub Actions runs on Linux, macOS, and Windows, including native locking and permission checks, and verifies Windows startup with the official npm CLI. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## References

- [Bitwarden CLI](https://bitwarden.com/help/cli/)
- [Bitwarden file attachments](https://bitwarden.com/help/attachments/)
- [1Password unencrypted export format](https://support.1password.com/1pux-format/)
