"""Tests for before/after system state snapshots and diffing."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.state_diff import (
    _read,
    _scan_dir,
    diff_snapshots,
    format_side_effects,
    take_snapshot,
)


class TestReadHelper:
    def test_reads_and_strips(self, tmp_path):
        f = tmp_path / "brightness"
        f.write_text("2\n")
        assert _read(f) == "2"

    def test_missing_file_returns_none(self, tmp_path):
        assert _read(tmp_path / "nope") is None

    def test_unreadable_file_returns_none(self, tmp_path):
        d = tmp_path / "a_directory"
        d.mkdir()
        assert _read(d) is None


class TestScanDir:
    def test_missing_class_dir_yields_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.state_diff._SYS_CLASS", tmp_path)
        assert _scan_dir("leds", ("brightness",)) == {}

    def test_collects_requested_attributes(self, tmp_path, monkeypatch):
        device = tmp_path / "leds" / "kbd_backlight"
        device.mkdir(parents=True)
        (device / "brightness").write_text("2\n")
        (device / "max_brightness").write_text("3\n")
        monkeypatch.setattr("src.state_diff._SYS_CLASS", tmp_path)

        assert _scan_dir("leds", ("brightness", "max_brightness")) == {
            "leds/kbd_backlight": {"brightness": "2", "max_brightness": "3"}
        }

    def test_devices_without_the_attributes_are_omitted(self, tmp_path, monkeypatch):
        (tmp_path / "leds" / "boring").mkdir(parents=True)
        monkeypatch.setattr("src.state_diff._SYS_CLASS", tmp_path)
        assert _scan_dir("leds", ("brightness",)) == {}

    def test_absent_attributes_are_simply_missing(self, tmp_path, monkeypatch):
        device = tmp_path / "leds" / "kbd"
        device.mkdir(parents=True)
        (device / "brightness").write_text("1\n")
        monkeypatch.setattr("src.state_diff._SYS_CLASS", tmp_path)
        assert _scan_dir("leds", ("brightness", "max_brightness")) == {
            "leds/kbd": {"brightness": "1"}
        }


class TestTakeSnapshot:
    def test_covers_leds_backlight_and_power_supply(self, tmp_path, monkeypatch):
        for subdir, attr, value in [
            ("leds", "brightness", "1"),
            ("backlight", "brightness", "500"),
            ("power_supply", "status", "Discharging"),
        ]:
            device = tmp_path / subdir / "dev0"
            device.mkdir(parents=True)
            (device / attr).write_text(f"{value}\n")
        monkeypatch.setattr("src.state_diff._SYS_CLASS", tmp_path)

        snapshot = take_snapshot()
        assert set(snapshot) == {"leds/dev0", "backlight/dev0", "power_supply/dev0"}

    def test_empty_system_yields_an_empty_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.state_diff._SYS_CLASS", tmp_path)
        assert take_snapshot() == {}


class TestDiffSnapshots:
    def test_identical_snapshots_have_no_changes(self):
        snap = {"leds/kbd": {"brightness": "1"}}
        assert diff_snapshots(snap, snap) == {}

    def test_changed_value_is_reported_as_before_after(self):
        before = {"leds/kbd": {"brightness": "1"}}
        after = {"leds/kbd": {"brightness": "0"}}
        assert diff_snapshots(before, after) == {"leds/kbd": {"brightness": ("1", "0")}}

    def test_appearing_device_is_not_reported(self):
        """Only attributes present on both sides can be diffed."""
        assert diff_snapshots({}, {"leds/kbd": {"brightness": "1"}}) == {}

    def test_disappearing_device_is_not_reported(self):
        assert diff_snapshots({"leds/kbd": {"brightness": "1"}}, {}) == {}

    def test_unchanged_attributes_are_excluded_from_a_changed_device(self):
        before = {"leds/kbd": {"brightness": "1", "max_brightness": "3"}}
        after = {"leds/kbd": {"brightness": "0", "max_brightness": "3"}}
        assert diff_snapshots(before, after) == {"leds/kbd": {"brightness": ("1", "0")}}

    def test_multiple_devices_change_independently(self):
        before = {"leds/kbd": {"brightness": "1"}, "backlight/x": {"brightness": "5"}}
        after = {"leds/kbd": {"brightness": "0"}, "backlight/x": {"brightness": "5"}}
        assert set(diff_snapshots(before, after)) == {"leds/kbd"}


class TestFormatSideEffects:
    def test_no_changes_formats_to_nothing(self):
        assert format_side_effects({}) == []

    def test_renders_an_arrow_per_attribute(self):
        lines = format_side_effects({"leds/kbd": {"brightness": ("1", "0")}})
        assert lines == ["  leds/kbd/brightness: 1 → 0"]

    def test_output_is_sorted_and_one_line_per_attribute(self):
        lines = format_side_effects(
            {
                "leds/b": {"brightness": ("1", "0")},
                "leds/a": {"brightness": ("2", "3"), "max_brightness": ("3", "4")},
            }
        )
        assert lines == [
            "  leds/a/brightness: 2 → 3",
            "  leds/a/max_brightness: 3 → 4",
            "  leds/b/brightness: 1 → 0",
        ]
