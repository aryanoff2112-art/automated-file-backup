import os
import shutil
import datetime
import hashlib
import fnmatch
import argparse
import json
import sys
import time
import logging
import schedule

SOURCE_DIR = r"/path/to/source"
DESTINATION_DIR = r"/path/to/destination"

BACKUP_TIME = "00:00"

RETENTION_DAYS = 30        
CHECKSUM_ALGO = "sha256"
CHUNK_SIZE = 1024 * 1024   

EXCLUDED_DIRS = {".git", "__pycache__", "node_modules"}
EXCLUDED_EXTENSIONS = {".tmp", ".log"}
BACKUPIGNORE_FILENAME = ".backupignore"

INCOMPLETE_PREFIX = ".backup_"
INCOMPLETE_SUFFIX = ".incomplete"
COMPLETE_PREFIX = "backup_"

logging.basicConfig(
    filename="backup.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

def validate_directories(source_dir, destination_dir):
    """Raise ValueError if destination_dir is inside source_dir (which would
    make the backup eventually walk into and back up its own backups)."""
    source_real = os.path.realpath(source_dir)
    destination_real = os.path.realpath(destination_dir)

    if source_real == destination_real:
        raise ValueError("DESTINATION_DIR cannot be the same as SOURCE_DIR.")

    common = os.path.commonpath([source_real, destination_real])
    if common == source_real:
        raise ValueError("DESTINATION_DIR cannot be inside SOURCE_DIR.")

def load_backupignore_patterns(source_dir):
    ignore_path = os.path.join(source_dir, BACKUPIGNORE_FILENAME)
    patterns = []
    if os.path.isfile(ignore_path):
        with open(ignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns

def is_excluded(rel_path, filename, patterns,
                 excluded_dirs=None, excluded_extensions=None):
    excluded_dirs = EXCLUDED_DIRS if excluded_dirs is None else excluded_dirs
    excluded_extensions = EXCLUDED_EXTENSIONS if excluded_extensions is None else excluded_extensions

    _, ext = os.path.splitext(filename)
    if ext in excluded_extensions:
        return True

    parts = rel_path.replace(os.sep, "/").split("/")
    if any(part in excluded_dirs for part in parts[:-1]):
        return True

    normalized = rel_path.replace(os.sep, "/")
    for pattern in patterns:
        pattern = pattern.rstrip("/")
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(filename, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts[:-1]):
            return True

    return False

def file_checksum(path, algo=CHECKSUM_ALGO):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def files_are_equivalent(src_path, dst_path, mode):

    try:
        src_stat = os.stat(src_path)
        dst_stat = os.stat(dst_path)
    except FileNotFoundError:
        return False, None

    if mode == "checksum":
        src_hash = file_checksum(src_path)
        dst_hash = file_checksum(dst_path)
        return src_hash == dst_hash, src_hash

    same_size = src_stat.st_size == dst_stat.st_size
    same_mtime = int(src_stat.st_mtime) == int(dst_stat.st_mtime)
    return (same_size and same_mtime), None

def find_previous_backup(destination_dir):

    if not os.path.isdir(destination_dir):
        return None
    candidates = [
        os.path.join(destination_dir, name)
        for name in os.listdir(destination_dir)
        if name.startswith(COMPLETE_PREFIX) and os.path.isdir(os.path.join(destination_dir, name))
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def cleanup_stale_incomplete_backups(destination_dir):

    if not os.path.isdir(destination_dir):
        return
    for name in os.listdir(destination_dir):
        if name.startswith(INCOMPLETE_PREFIX) and name.endswith(INCOMPLETE_SUFFIX):
            full_path = os.path.join(destination_dir, name)
            try:
                shutil.rmtree(full_path)
                logging.warning(f"Removed stale incomplete backup from a previous run: {full_path}")
                print(f"Removed stale incomplete backup from a previous run: {full_path}")
            except OSError as error:
                logging.error(f"Failed to remove stale incomplete backup {full_path}: {error}")

def load_manifest(backup_dir):
    manifest_path = os.path.join(backup_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return None
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_manifest(backup_dir, manifest):
    manifest_path = os.path.join(backup_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

def prune_old_backups(destination_dir, retention_days):
    if retention_days is None:
        return
    cutoff = time.time() - retention_days * 86400
    if not os.path.isdir(destination_dir):
        return

    for name in os.listdir(destination_dir):
        if not name.startswith(COMPLETE_PREFIX):
            continue
        full_path = os.path.join(destination_dir, name)
        try:
            mtime = os.path.getmtime(full_path)
        except OSError:
            continue

        if mtime < cutoff:
            try:
                if os.path.isdir(full_path):
                    shutil.rmtree(full_path)
                else:
                    os.remove(full_path)
                logging.info(f"Pruned old backup: {full_path}")
                print(f"Pruned old backup: {full_path}")
            except OSError as error:
                logging.error(f"Failed to prune {full_path}: {error}")
                print(f"Failed to prune {full_path}: {error}")

def run_backup(source_dir=None, destination_dir=None, dry_run=False, mode="fast",
                retention_days=None):

    source_dir = SOURCE_DIR if source_dir is None else source_dir
    destination_dir = DESTINATION_DIR if destination_dir is None else destination_dir
    retention_days = RETENTION_DAYS if retention_days is None else retention_days

    validate_directories(source_dir, destination_dir)

    start_time = time.time()
    stats = {
        "scanned": 0, "copied": 0, "linked": 0,
        "skipped": 0, "failed": 0, "bytes_copied": 0,
    }

    if not os.path.exists(source_dir):
        logging.error(f"Source directory does not exist: {source_dir}")
        print("Source directory does not exist.")
        return {"status": "source_missing", "stats": stats, "backup_dir": None}

    if not dry_run:
        os.makedirs(destination_dir, exist_ok=True)
        cleanup_stale_incomplete_backups(destination_dir)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    final_name = f"{COMPLETE_PREFIX}{timestamp}"
    temp_name = f"{INCOMPLETE_PREFIX}{timestamp}{INCOMPLETE_SUFFIX}"
    destination = os.path.join(destination_dir, final_name)
    working_dir = destination if dry_run else os.path.join(destination_dir, temp_name)

    previous_backup = find_previous_backup(destination_dir)
    previous_manifest = load_manifest(previous_backup) if previous_backup else None
    previous_checksums = (previous_manifest or {}).get("checksums", {})

    ignore_patterns = load_backupignore_patterns(source_dir)
    checksums = {}

    print("Backup started...")
    logging.info(f"Backup started (mode={mode}). Previous backup: {previous_backup or 'none'}")

    try:
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]

            rel_dir = os.path.relpath(root, source_dir)
            dest_dir = os.path.join(working_dir, rel_dir) if rel_dir != "." else working_dir

            for filename in files:
                rel_path = os.path.join(rel_dir, filename) if rel_dir != "." else filename
                stats["scanned"] += 1

                if is_excluded(rel_path, filename, ignore_patterns):
                    stats["skipped"] += 1
                    continue

                if not dry_run:
                    os.makedirs(dest_dir, exist_ok=True)

                src_path = os.path.join(root, filename)
                dst_path = os.path.join(dest_dir, filename)
                prev_path = os.path.join(previous_backup, rel_path) if previous_backup else None

                try:
                    unchanged, computed_hash = (
                        files_are_equivalent(src_path, prev_path, mode)
                        if prev_path else (False, None)
                    )

                    if unchanged:
                        if not dry_run:
                            try:
                                os.link(prev_path, dst_path)
                                stats["linked"] += 1
                                checksums[rel_path.replace(os.sep, "/")] = (
                                    computed_hash
                                    or previous_checksums.get(rel_path.replace(os.sep, "/"))
                                    or file_checksum(dst_path)
                                )
                            except OSError:
                                shutil.copy2(src_path, dst_path)
                                stats["copied"] += 1
                                stats["bytes_copied"] += os.path.getsize(src_path)
                                checksums[rel_path.replace(os.sep, "/")] = file_checksum(dst_path)
                        else:
                            stats["linked"] += 1
                        continue

                    if not dry_run:
                        shutil.copy2(src_path, dst_path)
                        src_hash = computed_hash or file_checksum(src_path)
                        dst_hash = file_checksum(dst_path)
                        if src_hash != dst_hash:
                            raise IOError(f"Checksum mismatch after copy: {src_path}")
                        checksums[rel_path.replace(os.sep, "/")] = dst_hash

                    stats["copied"] += 1
                    stats["bytes_copied"] += os.path.getsize(src_path)

                except PermissionError:
                    stats["failed"] += 1
                    logging.error(f"Permission denied: {src_path}")
                    print(f"Permission denied: {src_path}")

                except OSError as error:
                    stats["failed"] += 1
                    logging.error(f"Failed to back up {src_path}: {error}")
                    print(f"Failed to back up {src_path}: {error}")

        duration = time.time() - start_time

        if dry_run:
            summary = (
                f"[DRY RUN] scanned={stats['scanned']} would_copy={stats['copied']} "
                f"would_link={stats['linked']} would_skip={stats['skipped']} "
                f"would_fail={stats['failed']} "
                f"({stats['bytes_copied'] / (1024*1024):.2f} MB would be copied) "
                f"in {duration:.2f}s"
            )
            print(summary)
            logging.info(summary)
            return {"status": "dry_run", "stats": stats, "backup_dir": None}

        status = "success" if stats["failed"] == 0 else "partial_failure"

        manifest = {
            "backup_id": final_name,
            "created_at": datetime.datetime.now().isoformat(),
            "source": source_dir,
            "mode": mode,
            "files_scanned": stats["scanned"],
            "files_copied": stats["copied"],
            "files_linked": stats["linked"],
            "files_skipped": stats["skipped"],
            "files_failed": stats["failed"],
            "bytes_copied": stats["bytes_copied"],
            "duration_seconds": round(duration, 2),
            "status": status,
            "checksums": checksums,
        }
        write_manifest(working_dir, manifest)

        os.rename(working_dir, destination)

    except Exception as error:  # noqa: BLE001 -- a genuine crash, not a per-file error
        logging.error(f"Backup crashed mid-run: {error}")
        print(f"Backup crashed: {error}")
        print(f"Incomplete data left at: {working_dir} (will be cleaned up on next run)")
        return {"status": "crashed", "stats": stats, "backup_dir": None}

    summary = (
        f"Backup completed: {destination} | "
        f"scanned={stats['scanned']} copied={stats['copied']} linked={stats['linked']} "
        f"skipped={stats['skipped']} failed={stats['failed']} "
        f"data_copied={stats['bytes_copied'] / (1024*1024):.2f} MB "
        f"in {duration:.2f}s"
    )
    print(summary)
    logging.info(summary)

    prune_old_backups(destination_dir, retention_days)

    return {"status": status, "stats": stats, "backup_dir": destination}

def verify_backup(destination_dir, backup_name):
    """Returns True if every checksummed file matches, False otherwise."""
    backup_dir = os.path.join(destination_dir, backup_name)
    manifest = load_manifest(backup_dir)

    if manifest is None:
        print(f"No manifest found for {backup_dir}")
        return False

    checksums = manifest.get("checksums", {})
    total = len(checksums)
    ok = 0
    bad = []

    print(f"Verifying {backup_name} ({total} files)...")
    for rel_path, expected_hash in checksums.items():
        full_path = os.path.join(backup_dir, rel_path.replace("/", os.sep))
        if not os.path.isfile(full_path):
            bad.append((rel_path, "missing"))
            continue
        actual_hash = file_checksum(full_path)
        if actual_hash == expected_hash:
            ok += 1
        else:
            bad.append((rel_path, "checksum mismatch"))

    if bad:
        print(f"\u2717 {len(bad)} problem(s) found:")
        for rel_path, reason in bad:
            print(f"  - {rel_path}: {reason}")
        return False

    print(f"\u2713 {ok}/{total} files verified. Integrity check passed.")
    return True

def list_backups(destination_dir):
    if not os.path.isdir(destination_dir):
        print("No backups found.")
        return []
    names = sorted(
        n for n in os.listdir(destination_dir)
        if n.startswith(COMPLETE_PREFIX) and os.path.isdir(os.path.join(destination_dir, n))
    )
    if not names:
        print("No backups found.")
        return []
    manifests = []
    for name in names:
        manifest = load_manifest(os.path.join(destination_dir, name))
        manifests.append(manifest)
        if manifest:
            print(
                f"{name}  status={manifest.get('status')}  "
                f"copied={manifest.get('files_copied')}  linked={manifest.get('files_linked')}  "
                f"failed={manifest.get('files_failed')}"
            )
        else:
            print(f"{name}  (no manifest)")
    return manifests

def iter_backup_files(backup_dir):
    for root, _dirs, files in os.walk(backup_dir):
        for filename in files:
            if filename == "manifest.json":
                continue
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, backup_dir).replace(os.sep, "/")
            yield rel_path

def restore_backup(destination_dir, backup_name, files=None, target=None, force=False,
                    source_dir=None, prompt=input):
    """Returns True on a clean, complete restore; False otherwise."""
    source_dir = SOURCE_DIR if source_dir is None else source_dir
    backup_dir = os.path.join(destination_dir, backup_name)

    if not os.path.isdir(backup_dir) or not os.path.isfile(os.path.join(backup_dir, "manifest.json")):
        print(f"Backup not found or incomplete: {backup_dir}")
        return False

    restoring_into_source = target is None
    target_dir = target or source_dir

    if files:
        available = set(iter_backup_files(backup_dir))
        missing = [f for f in files if f not in available]
        if missing:
            print("These files were not found in the backup:")
            for m in missing:
                print(f"  - {m}")
            files = [f for f in files if f in available]
            if not files:
                print("Nothing to restore.")
                return False
        rel_paths = files
    else:
        rel_paths = list(iter_backup_files(backup_dir))

    if not force:
        scope = f"{len(rel_paths)} file(s)" if files else "the ENTIRE backup"
        warning = " (this is the LIVE source directory)" if restoring_into_source else ""
        print(f"About to restore {scope} from {backup_name} into {target_dir}{warning}")
        confirm = prompt("Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Restore cancelled.")
            return False

    restored = overwritten = skipped = failed = 0

    for rel_path in rel_paths:
        src_path = os.path.join(backup_dir, rel_path.replace("/", os.sep))
        dst_path = os.path.join(target_dir, rel_path.replace("/", os.sep))
        existed = os.path.exists(dst_path)

        if existed and not force:
            answer = prompt(f"'{rel_path}' already exists at destination. Overwrite? [y/N] ").strip().lower()
            if answer != "y":
                skipped += 1
                continue

        try:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(src_path, dst_path)
            restored += 1
            if existed:
                overwritten += 1
        except OSError as error:
            failed += 1
            logging.error(f"Restore failed for {rel_path}: {error}")
            print(f"Failed to restore {rel_path}: {error}")

    summary = (
        f"Restore complete: restored={restored} (overwritten={overwritten}) "
        f"skipped={skipped} failed={failed}"
    )
    print(summary)
    logging.info(f"Restore from {backup_name} into {target_dir}: {summary}")
    return failed == 0

def build_parser():
    parser = argparse.ArgumentParser(prog="backupforge", description="Incremental, verifiable backups.")
    sub = parser.add_subparsers(dest="command")

    p_backup = sub.add_parser("backup", help="Run a backup now.")
    p_backup.add_argument("--dry-run", action="store_true", help="Report what would happen without touching the filesystem.")
    p_backup.add_argument("--mode", choices=["fast", "checksum"], default="fast",
                           help="fast = size+mtime comparison (default). checksum = hash every file (slower, exact).")

    p_verify = sub.add_parser("verify", help="Verify a backup against its manifest checksums.")
    p_verify.add_argument("backup_name", help="e.g. backup_2026-09-01_00-00-00")

    sub.add_parser("list", help="List available backups.")

    p_restore = sub.add_parser("restore", help="Restore a backup, or specific files from it.")
    p_restore.add_argument("backup_name", help="e.g. backup_2026-09-01_00-00-00")
    p_restore.add_argument("--files", nargs="+", metavar="RELATIVE_PATH",
                            help="Restrict to these files (relative paths, e.g. sub/notes.txt). Omit to restore the whole backup.")
    p_restore.add_argument("--target", metavar="DIR",
                            help="Where to restore to. Defaults to SOURCE_DIR (restores in place, overwriting live files) -- prefer an explicit --target when possible.")
    p_restore.add_argument("--force", action="store_true", help="Skip confirmation prompts and overwrite without asking.")

    sub.add_parser("prune", help="Delete backups older than RETENTION_DAYS.")

    sub.add_parser("schedule", help="Run the daily scheduler loop (default if no subcommand is given).")

    return parser

def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "verify":
            ok = verify_backup(DESTINATION_DIR, args.backup_name)
            return 0 if ok else 3

        if args.command == "list":
            list_backups(DESTINATION_DIR)
            return 0

        if args.command == "restore":
            ok = restore_backup(DESTINATION_DIR, args.backup_name, files=args.files,
                                 target=args.target, force=args.force, source_dir=SOURCE_DIR)
            return 0 if ok else 1

        if args.command == "prune":
            validate_directories(SOURCE_DIR, DESTINATION_DIR)
            prune_old_backups(DESTINATION_DIR, RETENTION_DAYS)
            return 0

        if args.command == "backup":
            result = run_backup(dry_run=args.dry_run, mode=args.mode)
            if result["status"] in ("success", "dry_run"):
                return 0
            return 1

        validate_directories(SOURCE_DIR, DESTINATION_DIR)
        schedule.every().day.at(BACKUP_TIME).do(run_backup)
        print(f"Automated backup scheduled daily at {BACKUP_TIME}")
        while True:
            schedule.run_pending()
            time.sleep(1)

    except ValueError as error:
        print(f"Configuration error: {error}")
        return 2

if __name__ == "__main__":
    sys.exit(main())