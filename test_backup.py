import os
import sys
import time
import json

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import backup  

@pytest.fixture
def source(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    return src

@pytest.fixture
def destination(tmp_path):
    return tmp_path / "destination"

def write(path, content="hello"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

def test_checksum_is_deterministic(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("some content")
    assert backup.file_checksum(str(f)) == backup.file_checksum(str(f))

def test_checksum_differs_for_different_content(tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("content one")
    f2.write_text("content two")
    assert backup.file_checksum(str(f1)) != backup.file_checksum(str(f2))

def test_excluded_extension():
    assert backup.is_excluded("debug.log", "debug.log", [],
                               excluded_extensions={".log"}) is True

def test_excluded_dir():
    assert backup.is_excluded("__pycache__/x.pyc", "x.pyc", [],
                               excluded_dirs={"__pycache__"}) is True

def test_not_excluded_by_default():
    assert backup.is_excluded("notes.txt", "notes.txt", [],
                               excluded_dirs=set(), excluded_extensions=set()) is False

def test_backupignore_pattern_matches():
    patterns = ["*.bak"]
    assert backup.is_excluded("notes.bak", "notes.bak", patterns,
                               excluded_dirs=set(), excluded_extensions=set()) is True

def test_backupignore_loaded_from_source(source):
    (source / ".backupignore").write_text("*.bak\n# comment\n")
    patterns = backup.load_backupignore_patterns(str(source))
    assert "*.bak" in patterns
    assert "# comment" not in patterns

def test_fast_mode_same_size_and_mtime_is_equivalent(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("identical")
    dst.write_text("identical")
    same_time = time.time()
    os.utime(src, (same_time, same_time))
    os.utime(dst, (same_time, same_time))

    equivalent, _ = backup.files_are_equivalent(str(src), str(dst), mode="fast")
    assert equivalent is True

def test_fast_mode_different_mtime_is_not_equivalent(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("identical")
    dst.write_text("identical")
    os.utime(src, (1000, 1000))
    os.utime(dst, (2000, 2000))

    equivalent, _ = backup.files_are_equivalent(str(src), str(dst), mode="fast")
    assert equivalent is False

def test_checksum_mode_detects_content_difference_despite_same_mtime(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("real content")
    dst.write_text("different!")
    same_time = time.time()
    os.utime(src, (same_time, same_time))
    os.utime(dst, (same_time, same_time))

    equivalent, computed_hash = backup.files_are_equivalent(str(src), str(dst), mode="checksum")
    assert equivalent is False
    assert computed_hash is not None

def test_destination_inside_source_is_rejected(source):
    dest_inside = source / "backups"
    with pytest.raises(ValueError):
        backup.validate_directories(str(source), str(dest_inside))

def test_destination_outside_source_is_accepted(source, destination):
    backup.validate_directories(str(source), str(destination))  # should not raise

def test_same_source_and_destination_is_rejected(source):
    with pytest.raises(ValueError):
        backup.validate_directories(str(source), str(source))

def test_first_backup_copies_everything(source, destination):
    write(source / "a.txt", "hello")
    write(source / "sub" / "b.txt", "world")

    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))

    assert result["status"] == "success"
    assert result["stats"]["copied"] == 2
    assert result["stats"]["linked"] == 0
    assert os.path.isfile(os.path.join(result["backup_dir"], "a.txt"))
    assert os.path.isfile(os.path.join(result["backup_dir"], "sub", "b.txt"))

def test_second_backup_hardlinks_unchanged_files(source, destination):
    write(source / "a.txt", "hello")
    backup.run_backup(source_dir=str(source), destination_dir=str(destination))

    time.sleep(1.01)  
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))

    assert result["status"] == "success"
    assert result["stats"]["linked"] == 1
    assert result["stats"]["copied"] == 0

def test_changed_file_is_recopied_not_linked(source, destination):
    write(source / "a.txt", "version one")
    r1 = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    time.sleep(1.01)
    write(source / "a.txt", "version two")
    r2 = backup.run_backup(source_dir=str(source), destination_dir=str(destination))

    assert r2["stats"]["copied"] == 1
    assert r2["stats"]["linked"] == 0
    content = open(os.path.join(r2["backup_dir"], "a.txt")).read()
    assert content == "version two"
    original = open(os.path.join(r1["backup_dir"], "a.txt")).read()
    assert original == "version one"

def test_hardlink_fallback_to_copy(source, destination, monkeypatch):
    write(source / "a.txt", "hello")
    backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    time.sleep(1.01)

    def broken_link(*args, **kwargs):
        raise OSError("simulated cross-device link failure")
    monkeypatch.setattr(backup.os, "link", broken_link)

    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    assert result["stats"]["copied"] == 1
    assert result["stats"]["linked"] == 0
    assert result["status"] == "success"

def test_exclusions_are_skipped_during_backup(source, destination):
    write(source / "a.txt", "hello")
    write(source / "debug.log", "should be skipped")

    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))

    assert result["stats"]["skipped"] == 1
    assert not os.path.exists(os.path.join(result["backup_dir"], "debug.log"))

def test_manifest_is_written_and_matches_stats(source, destination):
    write(source / "a.txt", "hello")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))

    manifest = backup.load_manifest(result["backup_dir"])
    assert manifest is not None
    assert manifest["files_copied"] == 1
    assert manifest["status"] == "success"
    assert "a.txt" in manifest["checksums"]

def test_dry_run_creates_no_backup_directory(source, destination):
    write(source / "a.txt", "hello")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination), dry_run=True)

    assert result["status"] == "dry_run"
    assert not os.path.isdir(destination)

def test_source_missing_reports_status(destination, tmp_path):
    missing_source = tmp_path / "does-not-exist"
    result = backup.run_backup(source_dir=str(missing_source), destination_dir=str(destination))
    assert result["status"] == "source_missing"

def test_backup_is_atomic_no_partial_dir_left_after_crash(source, destination, monkeypatch):
    write(source / "a.txt", "hello")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(backup, "write_manifest", boom)

    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    assert result["status"] == "crashed"

    if os.path.isdir(destination):
        visible = [n for n in os.listdir(destination) if n.startswith(backup.COMPLETE_PREFIX)]
        assert visible == []

def test_stale_incomplete_backup_is_cleaned_up_on_next_run(source, destination):
    os.makedirs(destination, exist_ok=True)
    stale = os.path.join(destination, ".backup_2020-01-01_00-00-00.incomplete")
    os.makedirs(stale)
    write(source / "a.txt", "hello")

    backup.run_backup(source_dir=str(source), destination_dir=str(destination))

    assert not os.path.isdir(stale)

def test_retention_prunes_old_backups(source, destination):
    write(source / "a.txt", "hello")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    old_backup_dir = result["backup_dir"]

    old_time = time.time() - (40 * 86400)
    os.utime(old_backup_dir, (old_time, old_time))

    backup.prune_old_backups(str(destination), retention_days=30)

    assert not os.path.isdir(old_backup_dir)

def test_retention_keeps_recent_backups(source, destination):
    write(source / "a.txt", "hello")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))

    backup.prune_old_backups(str(destination), retention_days=30)

    assert os.path.isdir(result["backup_dir"])

def test_verify_passes_on_untouched_backup(source, destination):
    write(source / "a.txt", "hello")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    backup_name = os.path.basename(result["backup_dir"])

    assert backup.verify_backup(str(destination), backup_name) is True

def test_verify_fails_on_tampered_file(source, destination):
    write(source / "a.txt", "hello")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    backup_name = os.path.basename(result["backup_dir"])

    with open(os.path.join(result["backup_dir"], "a.txt"), "a") as f:
        f.write("tampered")

    assert backup.verify_backup(str(destination), backup_name) is False

def test_verify_missing_backup_returns_false(destination):
    assert backup.verify_backup(str(destination), "backup_does_not_exist") is False

def test_restore_full_backup_into_target(source, destination, tmp_path):
    write(source / "a.txt", "hello")
    write(source / "sub" / "b.txt", "world")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    backup_name = os.path.basename(result["backup_dir"])

    restore_target = tmp_path / "restored"
    restore_target.mkdir()

    ok = backup.restore_backup(str(destination), backup_name, target=str(restore_target), force=True)

    assert ok is True
    assert (restore_target / "a.txt").read_text() == "hello"
    assert (restore_target / "sub" / "b.txt").read_text() == "world"

def test_restore_specific_file_only(source, destination, tmp_path):
    write(source / "a.txt", "hello")
    write(source / "sub" / "b.txt", "world")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    backup_name = os.path.basename(result["backup_dir"])

    restore_target = tmp_path / "restored"
    restore_target.mkdir()

    ok = backup.restore_backup(str(destination), backup_name, files=["sub/b.txt"],
                                target=str(restore_target), force=True)

    assert ok is True
    assert (restore_target / "sub" / "b.txt").exists()
    assert not (restore_target / "a.txt").exists()

def test_restore_missing_file_reports_and_skips(source, destination, tmp_path):
    write(source / "a.txt", "hello")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    backup_name = os.path.basename(result["backup_dir"])

    restore_target = tmp_path / "restored"
    restore_target.mkdir()

    ok = backup.restore_backup(str(destination), backup_name, files=["does/not/exist.txt"],
                                target=str(restore_target), force=True)

    assert ok is False

def test_restore_declines_overwrite_when_prompt_says_no(source, destination, tmp_path):
    write(source / "a.txt", "backed up content")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    backup_name = os.path.basename(result["backup_dir"])

    restore_target = tmp_path / "restored"
    restore_target.mkdir()
    (restore_target / "a.txt").write_text("local changes, do not clobber")

    backup.restore_backup(str(destination), backup_name, target=str(restore_target),
                           force=False, prompt=lambda _msg: "n")

    assert (restore_target / "a.txt").read_text() == "local changes, do not clobber"

def test_restore_accepts_overwrite_when_prompt_says_yes(source, destination, tmp_path):
    write(source / "a.txt", "backed up content")
    result = backup.run_backup(source_dir=str(source), destination_dir=str(destination))
    backup_name = os.path.basename(result["backup_dir"])

    restore_target = tmp_path / "restored"
    restore_target.mkdir()
    (restore_target / "a.txt").write_text("local changes")

    backup.restore_backup(str(destination), backup_name, target=str(restore_target),
                           force=False, prompt=lambda _msg: "y")

    assert (restore_target / "a.txt").read_text() == "backed up content"

def test_restore_rejects_incomplete_backup(destination, tmp_path):
    fake_incomplete = os.path.join(str(destination), "backup_fake")
    os.makedirs(fake_incomplete)  

    ok = backup.restore_backup(str(destination), "backup_fake", target=str(tmp_path / "out"), force=True)
    assert ok is False