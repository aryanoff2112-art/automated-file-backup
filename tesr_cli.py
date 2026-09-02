import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import backup  

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Points the module's global SOURCE_DIR/DESTINATION_DIR at temp dirs,
    the way editing the constants at the top of backup.py would in real use."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()

    monkeypatch.setattr(backup, "SOURCE_DIR", str(source))
    monkeypatch.setattr(backup, "DESTINATION_DIR", str(destination))

    return source, destination

def write(path, content="hello"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

def test_cli_backup_command_success(cli_env, capsys):
    source, _destination = cli_env
    write(source / "a.txt")

    exit_code = backup.main(["backup"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Backup completed" in out

def test_cli_backup_dry_run(cli_env, capsys):
    source, destination = cli_env
    write(source / "a.txt")

    exit_code = backup.main(["backup", "--dry-run"])

    assert exit_code == 0
    assert not os.path.isdir(destination)
    assert "DRY RUN" in capsys.readouterr().out

def test_cli_list_command(cli_env, capsys):
    source, _destination = cli_env
    write(source / "a.txt")
    backup.main(["backup"])

    exit_code = backup.main(["list"])

    assert exit_code == 0
    assert "backup_" in capsys.readouterr().out

def test_cli_verify_command_success(cli_env, capsys):
    source, destination = cli_env
    write(source / "a.txt")
    backup.main(["backup"])
    backup_name = sorted(os.listdir(destination))[0]

    exit_code = backup.main(["verify", backup_name])

    assert exit_code == 0
    assert "Integrity check passed" in capsys.readouterr().out

def test_cli_verify_command_failure_exit_code(cli_env):
    source, destination = cli_env
    write(source / "a.txt")
    backup.main(["backup"])
    backup_name = sorted(os.listdir(destination))[0]

    with open(os.path.join(str(destination), backup_name, "a.txt"), "a") as f:
        f.write("tampered")

    exit_code = backup.main(["verify", backup_name])

    assert exit_code == 3

def test_cli_verify_nonexistent_backup_exit_code(cli_env):
    exit_code = backup.main(["verify", "backup_does_not_exist"])
    assert exit_code == 3

def test_cli_restore_command(cli_env, tmp_path):
    source, destination = cli_env
    write(source / "a.txt", "backed up")
    backup.main(["backup"])
    backup_name = sorted(os.listdir(destination))[0]

    restore_target = tmp_path / "restored"
    restore_target.mkdir()

    exit_code = backup.main(["restore", backup_name, "--target", str(restore_target), "--force"])

    assert exit_code == 0
    assert (restore_target / "a.txt").read_text() == "backed up"

def test_cli_prune_command(cli_env):
    source, _destination = cli_env
    write(source / "a.txt")
    backup.main(["backup"])

    exit_code = backup.main(["prune"])

    assert exit_code == 0

def test_cli_backup_configuration_error_exit_code(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    nested_destination = source / "backups"  # inside source -- should be rejected

    monkeypatch.setattr(backup, "SOURCE_DIR", str(source))
    monkeypatch.setattr(backup, "DESTINATION_DIR", str(nested_destination))

    exit_code = backup.main(["backup"])

    assert exit_code == 2

def test_cli_backup_source_missing_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "SOURCE_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(backup, "DESTINATION_DIR", str(tmp_path / "destination"))

    exit_code = backup.main(["backup"])

    assert exit_code == 1

def test_cli_no_args_shows_help_not_crash(capsys):

    parser = backup.build_parser()
    args = parser.parse_args([])
    assert args.command is None