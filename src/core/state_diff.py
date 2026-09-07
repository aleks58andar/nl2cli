"""Before/after state snapshot for detecting unintended side effects.

Takes a lightweight snapshot of observable system state (sysfs hardware
controls, running services) before plan execution, then diffs against a
second snapshot after execution to surface unexpected changes.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SYS_CLASS = Path("/sys/class")

# sysfs attributes worth tracking for diffs
_LED_ATTRS = ("brightness", "max_brightness")
_BL_ATTRS = ("brightness", "actual_brightness")
_PS_ATTRS = ("status",)


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except Exception:
        return None


def _scan_dir(subdir: str, attrs: tuple[str, ...]) -> dict[str, dict[str, str]]:
    d = _SYS_CLASS / subdir
    if not d.is_dir():
        return {}
    result: dict[str, dict[str, str]] = {}
    for entry in sorted(d.iterdir()):
        info: dict[str, str] = {}
        for attr in attrs:
            v = _read(entry / attr)
            if v is not None:
                info[attr] = v
        if info:
            result[f"{subdir}/{entry.name}"] = info
    return result


def _get_active_services() -> dict[str, str]:
    """Get a subset of systemd service states (quick, relevant ones)."""
    try:
        proc = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True, text=True, timeout=5,
        )
        return {"systemd": proc.stdout.strip()}
    except Exception:
        return {}


def take_snapshot() -> dict[str, dict[str, str]]:
    """Capture a lightweight snapshot of observable system state.

    Returns a flat dict: key = "leds/tpacpi::kbd_backlight" etc,
    value = {"brightness": "2", ...}
    """
    snap: dict[str, dict[str, str]] = {}
    snap.update(_scan_dir("leds", _LED_ATTRS))
    snap.update(_scan_dir("backlight", _BL_ATTRS))
    snap.update(_scan_dir("power_supply", _PS_ATTRS))
    return snap


SideFx = dict[str, tuple[str, str]]  # attr → (before, after)


def diff_snapshots(
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
) -> dict[str, SideFx]:
    """Compare two snapshots and return changed entries.

    Returns: { "leds/tpacpi::kbd_backlight": {"brightness": ("2", "0")} }
    """
    changes: dict[str, SideFx] = {}

    all_keys = set(before.keys()) | set(after.keys())
    for key in sorted(all_keys):
        b = before.get(key, {})
        a = after.get(key, {})
        all_attrs = set(b.keys()) | set(a.keys())
        entry_changes: SideFx = {}
        for attr in sorted(all_attrs):
            bv = b.get(attr)
            av = a.get(attr)
            if bv != av and bv is not None and av is not None:
                entry_changes[attr] = (bv, av)
        if entry_changes:
            changes[key] = entry_changes

    return changes


def format_side_effects(changes: dict[str, SideFx]) -> list[str]:
    """Format changed entries into human-readable lines for logging/notification."""
    lines: list[str] = []
    for key, attrs in sorted(changes.items()):
        for attr, (bv, av) in sorted(attrs.items()):
            lines.append(f"  {key}/{attr}: {bv} → {av}")
    return lines
