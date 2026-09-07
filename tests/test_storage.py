"""Tests for the persistence layer: audit log and sysfs preferences.

Both modules write to fixed paths under $HOME, so every test redirects
those module-level paths at a tmp_path.
"""

import json
from pathlib import Path

import pytest

from src import audit, sysfs_prefs


@pytest.fixture
def history_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "state" / "nl2cli" / "history.jsonl"
    monkeypatch.setattr(audit, "HISTORY_FILE", path)
    return path


@pytest.fixture
def prefs_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "config" / "nl2cli" / "sysfs_prefs.json"
    monkeypatch.setattr(sysfs_prefs, "_PREFS_DIR", path.parent)
    monkeypatch.setattr(sysfs_prefs, "_PREFS_FILE", path)
    return path


def entry_kwargs(**overrides) -> dict:
    kwargs = {
        "transcript": "enable bluetooth",
        "plan": {"user_request": "enable bluetooth", "actions": []},
        "validator_findings": [],
        "confirmation_required": True,
        "confirmation_granted": True,
        "action_results": [],
    }
    kwargs.update(overrides)
    return kwargs


class TestAuditLog:
    def test_creates_parent_directories(self, history_file):
        audit.write_audit_entry(**entry_kwargs())
        assert history_file.exists()

    def test_writes_one_json_object_per_line(self, history_file):
        audit.write_audit_entry(**entry_kwargs(transcript="first"))
        audit.write_audit_entry(**entry_kwargs(transcript="second"))

        lines = history_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert [json.loads(line)["transcript"] for line in lines] == ["first", "second"]

    def test_entry_carries_all_fields_and_a_timestamp(self, history_file):
        audit.write_audit_entry(**entry_kwargs())
        entry = json.loads(history_file.read_text())

        assert set(entry) == {
            "timestamp",
            "transcript",
            "plan",
            "validator_findings",
            "confirmation_required",
            "confirmation_granted",
            "action_results",
        }
        assert entry["timestamp"].endswith("+00:00")

    def test_returns_the_path_written(self, history_file):
        assert audit.write_audit_entry(**entry_kwargs()) == history_file

    def test_non_serialisable_values_do_not_raise(self, history_file):
        audit.write_audit_entry(**entry_kwargs(plan={"path": Path("/etc/hosts")}))
        assert json.loads(history_file.read_text())["plan"]["path"] == "/etc/hosts"

    def test_declined_confirmation_is_recorded(self, history_file):
        audit.write_audit_entry(**entry_kwargs(confirmation_granted=False))
        assert json.loads(history_file.read_text())["confirmation_granted"] is False


class TestFormatActionResult:
    def test_short_output_is_untouched(self):
        result = audit.format_action_result(
            description="run ls", returncode=0, duration_ms=12, stdout="ok"
        )
        assert result["stdout_clip"] == "ok"
        assert result["returncode"] == 0
        assert result["duration_ms"] == 12

    def test_long_stdout_is_clipped_with_an_ellipsis(self):
        result = audit.format_action_result(
            description="d", returncode=0, duration_ms=1, stdout="x" * 1000
        )
        assert len(result["stdout_clip"]) == audit._STDOUT_CLIP + 1
        assert result["stdout_clip"].endswith("…")

    def test_stderr_is_clipped_too(self):
        result = audit.format_action_result(
            description="d", returncode=1, duration_ms=1, stderr="e" * 1000
        )
        assert result["stderr_clip"].endswith("…")

    def test_output_at_the_limit_is_not_clipped(self):
        exact = "x" * audit._STDOUT_CLIP
        result = audit.format_action_result(
            description="d", returncode=0, duration_ms=1, stdout=exact
        )
        assert result["stdout_clip"] == exact

    def test_timeout_is_representable_as_a_null_returncode(self):
        result = audit.format_action_result(
            description="d", returncode=None, duration_ms=None
        )
        assert result["returncode"] is None
        assert result["duration_ms"] is None


class TestSysfsPrefsRoundTrip:
    def test_missing_file_loads_as_empty(self, prefs_file):
        assert sysfs_prefs._load() == {}

    def test_corrupt_file_loads_as_empty(self, prefs_file):
        prefs_file.parent.mkdir(parents=True)
        prefs_file.write_text("{not json")
        assert sysfs_prefs._load() == {}

    def test_save_then_load_round_trips(self, prefs_file):
        sysfs_prefs._save({"leds/kbd/brightness": "1"})
        assert sysfs_prefs._load() == {"leds/kbd/brightness": "1"}


class TestSaveDesiredValues:
    def test_stores_the_before_value(self, prefs_file):
        sysfs_prefs.save_desired_values(
            {"leds/tpacpi::kbd_backlight": {"brightness": ("1", "0")}}
        )
        assert sysfs_prefs._load() == {"leds/tpacpi::kbd_backlight/brightness": "1"}

    def test_ignores_backlight_paths(self, prefs_file):
        sysfs_prefs.save_desired_values(
            {"backlight/intel_backlight": {"brightness": ("4819", "19393")}}
        )
        assert sysfs_prefs._load() == {}

    def test_ignores_power_supply_paths(self, prefs_file):
        sysfs_prefs.save_desired_values({"power_supply/BAT0": {"status": ("a", "b")}})
        assert sysfs_prefs._load() == {}

    def test_later_writes_overwrite_earlier_ones(self, prefs_file):
        sysfs_prefs.save_desired_values({"leds/kbd": {"brightness": ("1", "0")}})
        sysfs_prefs.save_desired_values({"leds/kbd": {"brightness": ("2", "0")}})
        assert sysfs_prefs._load()["leds/kbd/brightness"] == "2"


class TestUpdatePreferencesToNewValues:
    def test_updates_a_tracked_key(self, prefs_file):
        sysfs_prefs._save({"leds/kbd/brightness": "1"})
        sysfs_prefs.update_preferences_to_new_values(
            {"leds/kbd": {"brightness": ("1", "3")}}
        )
        assert sysfs_prefs._load()["leds/kbd/brightness"] == "3"

    def test_does_not_add_untracked_keys(self, prefs_file):
        sysfs_prefs._save({})
        sysfs_prefs.update_preferences_to_new_values(
            {"leds/kbd": {"brightness": ("1", "3")}}
        )
        assert sysfs_prefs._load() == {}

    def test_ignored_paths_are_skipped(self, prefs_file):
        sysfs_prefs._save({"backlight/intel_backlight/brightness": "100"})
        sysfs_prefs.update_preferences_to_new_values(
            {"backlight/intel_backlight": {"brightness": ("100", "200")}}
        )
        assert sysfs_prefs._load()["backlight/intel_backlight/brightness"] == "100"


class TestQuickRestore:
    def test_restores_when_before_matches_the_saved_preference(
        self, prefs_file, monkeypatch
    ):
        sysfs_prefs._save({"leds/kbd/brightness": "1"})

        written: list[tuple[str, str]] = []
        monkeypatch.setattr(
            sysfs_prefs,
            "_write_sysfs",
            lambda path, value, sudo_manager=None: written.append((str(path), value))
            or True,
        )

        restored = sysfs_prefs.quick_restore_side_effects(
            {"leds/kbd": {"brightness": ("1", "0")}}
        )
        assert restored == ["leds/kbd/brightness"]
        assert written == [("/sys/class/leds/kbd/brightness", "1")]

    def test_no_restore_when_before_differs_from_the_preference(
        self, prefs_file, monkeypatch
    ):
        sysfs_prefs._save({"leds/kbd/brightness": "1"})
        monkeypatch.setattr(
            sysfs_prefs, "_write_sysfs", lambda *a, **k: pytest.fail("should not write")
        )
        assert (
            sysfs_prefs.quick_restore_side_effects(
                {"leds/kbd": {"brightness": ("2", "0")}}
            )
            == []
        )

    def test_no_restore_for_untracked_keys(self, prefs_file, monkeypatch):
        monkeypatch.setattr(
            sysfs_prefs, "_write_sysfs", lambda *a, **k: pytest.fail("should not write")
        )
        assert (
            sysfs_prefs.quick_restore_side_effects(
                {"leds/kbd": {"brightness": ("1", "0")}}
            )
            == []
        )

    def test_failed_write_is_not_reported_as_restored(self, prefs_file, monkeypatch):
        sysfs_prefs._save({"leds/kbd/brightness": "1"})
        monkeypatch.setattr(sysfs_prefs, "_write_sysfs", lambda *a, **k: False)
        assert (
            sysfs_prefs.quick_restore_side_effects(
                {"leds/kbd": {"brightness": ("1", "0")}}
            )
            == []
        )


@pytest.fixture
def fake_sysfs(monkeypatch):
    """Make /sys/class paths readable with a chosen value, leaving real files alone.

    sysfs_prefs reads its own JSON store through the same Path methods, so
    the fakes have to be scoped by prefix rather than patched wholesale.
    """
    real_exists, real_read_text = Path.exists, Path.read_text

    def install(value: str) -> None:
        def fake_exists(self):
            return True if str(self).startswith("/sys/class") else real_exists(self)

        def fake_read_text(self, *args, **kwargs):
            if str(self).startswith("/sys/class"):
                return value
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", fake_exists)
        monkeypatch.setattr(Path, "read_text", fake_read_text)

    return install


class TestRestoreDesiredValues:
    def test_empty_prefs_restore_nothing(self, prefs_file):
        assert sysfs_prefs.restore_desired_values() == 0

    def test_missing_sysfs_path_is_skipped(self, prefs_file):
        sysfs_prefs._save({"leds/definitely-not-a-real-device/brightness": "1"})
        assert sysfs_prefs.restore_desired_values() == 0

    def test_value_already_correct_is_not_rewritten(
        self, prefs_file, fake_sysfs, monkeypatch
    ):
        sysfs_prefs._save({"leds/kbd/brightness": "1"})
        fake_sysfs("1\n")
        monkeypatch.setattr(
            sysfs_prefs, "_write_sysfs", lambda *a, **k: pytest.fail("should not write")
        )
        assert sysfs_prefs.restore_desired_values() == 0

    def test_stale_value_is_restored(self, prefs_file, fake_sysfs, monkeypatch):
        sysfs_prefs._save({"leds/kbd/brightness": "1"})
        fake_sysfs("0\n")
        monkeypatch.setattr(sysfs_prefs, "_write_sysfs", lambda *a, **k: True)
        assert sysfs_prefs.restore_desired_values() == 1

    def test_failed_writes_are_not_counted(self, prefs_file, fake_sysfs, monkeypatch):
        sysfs_prefs._save({"leds/kbd/brightness": "1"})
        fake_sysfs("0\n")
        monkeypatch.setattr(sysfs_prefs, "_write_sysfs", lambda *a, **k: False)
        assert sysfs_prefs.restore_desired_values() == 0
