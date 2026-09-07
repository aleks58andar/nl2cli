"""Tests for file editing: edit modes, backups, and sysfs writes.

Every test edits a file under tmp_path; backups are redirected there too,
so nothing touches the real filesystem.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.execution.base import Result
from src.execution.filesystem import EditFileRunner
from src.core.schema import EditFileAction, EditMode

from .conftest import edit


@pytest.fixture
def backup_dir(tmp_path, monkeypatch) -> Path:
    """Redirect the backup directory away from $HOME."""
    path = tmp_path / "backups"
    path.mkdir()
    monkeypatch.setattr("src.execution.filesystem.ensure_backup_dir", lambda: path)
    return path


def run_edit(path: Path, **kwargs):
    """Build and run an EditFileRunner against a path, no backup by default."""
    kwargs.setdefault("backup", False)
    return EditFileRunner(edit(str(path), **kwargs)).run()


class TestEnsureKeyValue:
    def test_updates_an_existing_key(self, tmp_path):
        f = tmp_path / "sshd_config"
        f.write_text("Port 22\nPermitRootLogin no\n")
        result = run_edit(f, ensure_key="Port", ensure_value="2222")

        assert result.ok and result.changed
        assert "Port 2222" in f.read_text()
        assert "Port 22\n" not in f.read_text()

    def test_uncomments_and_sets_a_commented_key(self, tmp_path):
        f = tmp_path / "sshd_config"
        f.write_text("#Port 22\n")
        run_edit(f, ensure_key="Port", ensure_value="2222")
        assert f.read_text().strip() == "Port 2222"

    def test_appends_a_missing_key(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("Existing yes\n")
        run_edit(f, ensure_key="Port", ensure_value="2222")

        text = f.read_text()
        assert "Existing yes" in text
        assert "Port 2222" in text

    def test_other_lines_are_preserved(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("# a comment\nKeepMe yes\nPort 22\n")
        run_edit(f, ensure_key="Port", ensure_value="2222")

        text = f.read_text()
        assert "# a comment" in text
        assert "KeepMe yes" in text

    def test_equals_style_files_keep_equals(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("KEY=old\n")
        run_edit(f, ensure_key="KEY", ensure_value="new")
        assert "KEY = new" in f.read_text()

    def test_trailing_newline_is_preserved(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("Port 22\n")
        run_edit(f, ensure_key="Port", ensure_value="2222")
        assert f.read_text().endswith("\n")

    def test_ini_file_is_handled_by_configparser(self, tmp_path):
        f = tmp_path / "app.ini"
        f.write_text("[main]\nkey = old\n")
        run_edit(f, ensure_key="key", ensure_value="new")

        text = f.read_text()
        assert "[main]" in text
        assert "new" in text


class TestReplaceLine:
    def test_replaces_the_matching_line(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("Port 22\nOther yes\n")
        result = run_edit(
            f, mode=EditMode.replace_line, selector=r"^\s*#?\s*Port\s+", after="Port 2222"
        )

        assert result.ok and result.changed
        assert f.read_text() == "Port 2222\nOther yes\n"

    def test_replaces_every_match(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("Port 22\nPort 23\n")
        run_edit(f, mode=EditMode.replace_line, selector=r"^Port ", after="Port 2222")
        assert f.read_text() == "Port 2222\nPort 2222\n"

    def test_no_match_is_reported_as_unchanged(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("Other yes\n")
        result = run_edit(
            f, mode=EditMode.replace_line, selector=r"^Port ", after="Port 2222"
        )

        assert result.ok
        assert result.changed is False
        assert "No changes needed" in result.stdout
        assert f.read_text() == "Other yes\n"


class TestInsert:
    def test_inserts_after_the_selector(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("a\nb\n")
        run_edit(f, mode=EditMode.insert, selector=r"^a$", after="inserted")
        assert f.read_text() == "a\ninserted\nb\n"

    def test_appends_when_the_selector_does_not_match(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("a\nb\n")
        run_edit(f, mode=EditMode.insert, selector=r"^zzz$", after="inserted")
        assert f.read_text() == "a\nb\ninserted\n"

    def test_appends_when_there_is_no_selector(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("a\n")
        run_edit(f, mode=EditMode.insert, after="inserted")
        assert f.read_text() == "a\ninserted\n"


class TestFileCreation:
    def test_missing_file_is_created(self, tmp_path):
        f = tmp_path / "new.conf"
        result = run_edit(f, ensure_key="Port", ensure_value="2222")

        assert result.ok and result.changed
        assert "Created" in result.stdout
        assert f.exists()
        assert "Port 2222" in f.read_text()

    def test_missing_parent_directories_are_created(self, tmp_path):
        f = tmp_path / "deep" / "nested" / "new.conf"
        assert run_edit(f, ensure_key="K", ensure_value="v").ok
        assert f.exists()

    def test_editing_an_existing_file_says_edited(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("Port 22\n")
        assert "Edited" in run_edit(f, ensure_key="Port", ensure_value="2222").stdout


class TestBackups:
    def test_backup_is_written_before_editing(self, tmp_path, backup_dir):
        f = tmp_path / "conf"
        f.write_text("Port 22\n")
        EditFileRunner(edit(str(f), ensure_key="Port", ensure_value="2222")).run()

        backups = list(backup_dir.iterdir())
        assert len(backups) == 1
        assert backups[0].read_text() == "Port 22\n"

    def test_backup_name_encodes_path_and_timestamp(self, tmp_path, backup_dir):
        f = tmp_path / "conf"
        f.write_text("x\n")
        EditFileRunner(edit(str(f), ensure_key="K", ensure_value="v")).run()

        name = next(backup_dir.iterdir()).name
        assert name.endswith(".bak")
        assert "/" not in name
        assert "conf" in name

    def test_no_backup_when_disabled(self, tmp_path, backup_dir):
        f = tmp_path / "conf"
        f.write_text("x\n")
        EditFileRunner(
            edit(str(f), backup=False, ensure_key="K", ensure_value="v")
        ).run()
        assert list(backup_dir.iterdir()) == []

    def test_no_backup_for_a_file_that_does_not_exist_yet(self, tmp_path, backup_dir):
        EditFileRunner(
            edit(str(tmp_path / "new.conf"), ensure_key="K", ensure_value="v")
        ).run()
        assert list(backup_dir.iterdir()) == []


class TestSysfsWrites:
    @staticmethod
    def _sysfs_action(value: str = "2", **kwargs) -> EditFileAction:
        return EditFileAction(
            description="set brightness",
            path="/sys/class/leds/kbd/brightness",
            mode=EditMode.ensure_kv,
            ensure_key="brightness",
            ensure_value=value,
            backup=False,
            **kwargs,
        )

    def test_sysfs_paths_are_routed_to_the_raw_writer(self, monkeypatch):
        """A /sys path must bypass the config-file edit machinery entirely."""
        seen: list[str] = []
        monkeypatch.setattr(
            EditFileRunner,
            "_write_sysfs",
            lambda self, value: (seen.append(value), Result(ok=True))[1],
        )
        EditFileRunner(self._sysfs_action("2")).run()
        assert seen == ["2"]

    def test_raw_writer_writes_the_bare_value(self, tmp_path):
        """No key/value formatting — sysfs gets the value and a newline."""
        target = tmp_path / "brightness"
        target.write_text("0\n")

        runner = EditFileRunner(self._sysfs_action("2"))
        runner.path = target

        result = runner._write_sysfs("2")
        assert result.ok and result.changed
        assert target.read_text() == "2\n"

    def test_sysfs_write_without_a_value_fails(self):
        action = EditFileAction(
            description="set brightness",
            path="/sys/class/leds/kbd/brightness",
            mode=EditMode.ensure_kv,
            ensure_key="brightness",
            ensure_value="",
            backup=False,
        )
        result = EditFileRunner(action).run()
        assert result.ok is False
        assert "No value to write" in result.stderr

    def test_sysfs_write_with_sudo_uses_tee_on_stdin(self):
        manager = MagicMock()
        manager.run_sudo_command.return_value = MagicMock(returncode=0, stderr="")

        action = EditFileAction(
            description="set brightness",
            path="/sys/class/leds/kbd/brightness",
            mode=EditMode.ensure_kv,
            ensure_key="brightness",
            ensure_value="2",
            requires_sudo=True,
            backup=False,
        )
        result = EditFileRunner(action, manager).run()

        assert result.ok
        args, kwargs = manager.run_sudo_command.call_args
        assert args[0] == ["tee", "/sys/class/leds/kbd/brightness"]
        assert kwargs["input"] == "2"

    def test_failed_sudo_tee_is_reported(self):
        manager = MagicMock()
        manager.run_sudo_command.return_value = MagicMock(
            returncode=1, stderr="permission denied"
        )
        action = EditFileAction(
            description="set brightness",
            path="/sys/class/leds/kbd/brightness",
            mode=EditMode.ensure_kv,
            ensure_key="b",
            ensure_value="2",
            requires_sudo=True,
            backup=False,
        )
        result = EditFileRunner(action, manager).run()
        assert result.ok is False
        assert result.error_code == 1


class TestSudoFileWrites:
    def test_content_is_staged_in_a_temp_file_then_copied(self, tmp_path):
        target = tmp_path / "conf"
        target.write_text("Port 22\n")

        manager = MagicMock()
        manager.run_sudo_command.return_value = MagicMock(returncode=0, stderr="")

        result = EditFileRunner(
            edit(
                str(target),
                backup=False,
                requires_sudo=True,
                ensure_key="Port",
                ensure_value="2222",
            ),
            manager,
        ).run()

        assert result.ok and result.changed
        argv = manager.run_sudo_command.call_args[0][0]
        assert argv[0] == "cp"
        assert argv[2] == str(target)

    def test_failed_sudo_copy_is_reported(self, tmp_path):
        target = tmp_path / "conf"
        target.write_text("Port 22\n")

        manager = MagicMock()
        manager.run_sudo_command.return_value = MagicMock(returncode=1, stderr="nope")

        result = EditFileRunner(
            edit(
                str(target),
                backup=False,
                requires_sudo=True,
                ensure_key="Port",
                ensure_value="2222",
            ),
            manager,
        ).run()
        assert result.ok is False
        assert "Failed to write file with sudo" in result.stderr


class TestRunnerProperties:
    def test_description_and_sudo_flag_are_exposed(self, tmp_path):
        action = edit(str(tmp_path / "f"), description="edit the thing", requires_sudo=True)
        runner = EditFileRunner(action)
        assert runner.description == "edit the thing"
        assert runner.requires_sudo is True

    def test_dry_run_description(self, tmp_path):
        runner = EditFileRunner(edit(str(tmp_path / "f"), description="edit it"))
        assert runner.dry_run_description() == "Would execute: edit it"
