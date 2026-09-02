# BackupForge

[![Tests](https://github.com/aryanoff2112-art/automated-file-backup/actions/workflows/tests.yml/badge.svg)](https://github.com/aryanoff2112-art/automated-file-backup/actions/workflows/tests.yml)

Incremental, verifiable backups using hardlink snapshots — every backup
is a complete, browsable directory, but unchanged files cost almost no
extra disk space.


## How it works

### Hardlink snapshots

Each run of `backupforge backup` creates a new folder,
`backup_<timestamp>/`, that is a **complete, standalone copy of your
source directory** — you can open any single backup folder and see
every file, exactly as it looked at that point in time.

Under the hood, though, only files that actually **changed** since the
previous backup are copied. Files that are unchanged (same size and
modification time as the previous backup) are linked into the new
folder using a filesystem **hardlink** (`os.link`) instead of being
copied.

A hardlink is a second name for the *same* data on disk — not a
shortcut, not a reference, an actual second directory entry pointing
at the same bytes. That means:

- The new backup folder looks and behaves like a full copy.
- Deleting an old backup never corrupts a newer one — the operating
  system only frees the underlying data once *every* hardlink to it is
  gone.
- Disk usage only grows by the amount of data that actually changed
  between backups, even though every backup folder looks complete.

```
backup_2026-09-01_00-00-00/
├── documents/report.docx      ← original copy
└── photos/vacation.jpg        ← original copy

backup_2026-09-02_00-00-00/
├── documents/report.docx      ← hardlink to 2026-09-01's copy (unchanged)
└── photos/vacation.jpg        ← hardlink to 2026-09-01's copy (unchanged)
```

If you edit `report.docx` before the next backup, only that file is
recopied; `vacation.jpg` stays hardlinked.

**Caveat:** hardlinks generally only work within a single filesystem/
volume. `SOURCE_DIR` and `DESTINATION_DIR` can be on different drives —
what matters is that consecutive backup folders inside `DESTINATION_DIR`
are on the *same* volume as each other. If a hardlink can't be created
for any reason, BackupForge automatically falls back to a plain copy
for that file rather than failing the backup.

### Detecting changed files

By default (`--mode fast`), a file is considered unchanged if its size
and modification time match the previous backup — cheap to check, but
in principle a file could be modified in a way that preserves both
(rare, but possible with some sync tools).

`--mode checksum` instead hashes every file's content on both sides,
which is exact but slower on large trees. Use it when you want
certainty over speed.

### Verification

Every file that gets copied (not hardlinked) is immediately
checksummed (SHA-256) on both sides — if the copy doesn't match the
source, it's recorded as a failure rather than silently trusted.

Every backup also writes a `manifest.json` containing a checksum for
every file in that snapshot (hardlinked files reuse the checksum
recorded in the backup they were linked from, so this stays cheap).
You can re-verify a backup at any time — including long after it was
made — with:

```
backupforge verify backup_2026-09-01_00-00-00
```

This re-hashes every file in that backup and compares it against the
manifest, catching bit rot, accidental edits, or partial disk failures.

### What happens if a backup fails

Backups are written **atomically**. While a backup is in progress, its
files live in a hidden, clearly-marked folder:

```
.backup_2026-09-01_00-00-00.incomplete/
```

Only after every file has been copied/linked and the manifest has been
written successfully does BackupForge rename that folder to its real
name, `backup_2026-09-01_00-00-00/`. If the process is killed, crashes,
or the machine loses power partway through, you're left with only the
`.incomplete` folder — never something that looks like a valid,
complete backup but secretly isn't.

`backupforge backup` automatically deletes any leftover `.incomplete`
folder from a previous crashed run before starting a new one, so these
don't accumulate.

Individual file-level problems (a permission error on one file, a
transient read error) don't abort the whole backup — they're recorded
in the `failed` count and logged to `backup.log`, and the backup still
completes and becomes a valid snapshot for everything that *did*
succeed. The manifest's `status` field is `"success"` if nothing
failed, or `"partial_failure"` if some files did.

### Retention

Backups older than `RETENTION_DAYS` (default 30, set `None` to disable)
are deleted automatically after each successful backup, or on demand
with `backupforge prune`. Deleting an old backup is always safe with
respect to newer ones, thanks to how hardlinks work (see above).

### Exclusions

Three ways to exclude files from a backup:

1. `EXCLUDED_DIRS` in the config section (default: `.git`,
   `__pycache__`, `node_modules`) — matched directories are skipped
   entirely, including their contents.
2. `EXCLUDED_EXTENSIONS` (default: `.tmp`, `.log`).
3. A `.backupignore` file at the root of `SOURCE_DIR`, one glob pattern
   per line, `#` for comments — same idea as `.gitignore`:

   ```
   *.bak
   temp/
   secrets.env
   ```

Excluded files are counted in the `skipped` stat, not silently dropped.

## Installation

```bash
git clone https://github.com/aryanoff2112-art/automated-file-backup.git
cd automated-file-backup
pip install -e .
```

This installs the `backupforge` command via the `[project.scripts]`
entry point in `pyproject.toml`.

For development (running the test suite):

```bash
pip install -e ".[dev]"
```

## Configuration

BackupForge is currently configured by editing the constants at the
top of `backup.py`:

```python
SOURCE_DIR = r"/path/to/source"
DESTINATION_DIR = r"/path/to/destination"

BACKUP_TIME = "00:00"       
RETENTION_DAYS = 30          
CHECKSUM_ALGO = "sha256"

EXCLUDED_DIRS = {".git", "__pycache__", "node_modules"}
EXCLUDED_EXTENSIONS = {".tmp", ".log"}
```

*(A JSON/TOML config file and support for multiple source directories
are on the roadmap — see Limitations below.)*

## CLI usage

```bash

backupforge backup

backupforge backup --dry-run

backupforge backup --mode checksum

backupforge list

backupforge verify backup_2026-09-01_00-00-00

backupforge restore backup_2026-09-01_00-00-00

backupforge restore backup_2026-09-01_00-00-00 --target /tmp/recovered

backupforge restore backup_2026-09-01_00-00-00 --files sub/notes.txt

backupforge restore backup_2026-09-01_00-00-00 --force

backupforge prune

backupforge schedule
```

### Exit codes

| Code | Meaning                                    |
|------|---------------------------------------------|
| 0    | Success                                      |
| 1    | Backup or restore operation failed           |
| 2    | Invalid configuration (e.g. destination inside source) |
| 3    | Verification failed                          |

Useful for wiring into cron, Windows Task Scheduler, or a CI pipeline.

## Architecture

```
                    ┌──────────────────┐
                    │   SOURCE_DIR     │
                    └────────┬─────────┘
                             │ os.walk + exclusion rules
                             ▼
                  ┌────────────────────────┐
   previous ─────▶│ changed? (fast/checksum)│
   backup's       └──────────┬──────────────┘
   manifest             yes  │  no
                    ┌─────────┴─────────┐
                    ▼                   ▼
              copy + verify        hardlink to
              (sha256 check)       previous backup
                    │                   │
                    └─────────┬─────────┘
                               ▼
                 .backup_<ts>.incomplete/
                               │
                   write manifest.json
                               │
                          os.rename()
                               ▼
                    backup_<ts>/   ◀── now visible, restorable, verifiable
```

## Limitations

- **Single source directory.** Multiple independent source directories
  aren't supported yet — run separate `backupforge` configurations if
  you need that today.
- **No config file.** Settings are Python constants in `backup.py`,
  not an external `config.json`/`config.toml`.
- **No compression.** Deliberately left out: compressing a backup
  folder means deleting the folder hardlinks point into, which would
  break the next backup's ability to link against it. A compressed
  backup mode would need a separate, deduplicated storage design.
- **No encryption.** Backups are plain files with the same permissions
  as the source; there's no at-rest encryption.
- **Same-filesystem hardlinks.** Hardlinking only works within a single
  volume; across-volume links fall back to full copies automatically
  (correct, but loses the space-saving benefit for that file).
- **`fast` mode can theoretically miss a change** that preserves both
  file size and modification time. Use `--mode checksum` if you need
  certainty over speed.
- **No GUI or notifications.** Command-line only; no desktop/email
  alerts on success or failure (though everything is logged to
  `backup.log` and reflected in exit codes for scripting).

## Testing

```bash
pytest                                         
pytest --cov=backup --cov-report=term-missing   
```

53 tests cover checksum generation, exclusions/`.backupignore`, both
comparison modes, hardlink creation and its copy-fallback, atomic
backup creation (including simulated crashes), retention, manifest
generation, verification (clean and tampered), restore (full/partial/
missing-file/overwrite prompts), and the CLI commands themselves —
96% line coverage on `backup.py`.
