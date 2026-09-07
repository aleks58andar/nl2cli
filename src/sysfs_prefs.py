"""Persistent sysfs preferences — remembers desired hardware values.

When nl2cli detects an unintended side effect (e.g. keyboard backlight
reset to 0 by a power-profile change), the "before" value is recorded
here as the user's desired value.  On future executions nl2cli can:

  1. Restore desired values before taking snapshots (so diffs are accurate).
  2. Quick-restore known side effects without an LLM call.

The store is a small JSON file at ~/.config/nl2cli/sysfs_prefs.json:
  { "leds/tpacpi::kbd_backlight/brightness": "1" }
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_PREFS_DIR = Path.home() / ".config" / "nl2cli"
_PREFS_FILE = _PREFS_DIR / "sysfs_prefs.json"

SideFx = dict[str, tuple[str, str]]  # attr → (before, after)


def _load() -> dict[str, str]:
    if not _PREFS_FILE.exists():
        return {}
    try:
        return json.loads(_PREFS_FILE.read_text())
    except Exception:
        return {}


def _save(prefs: dict[str, str]) -> None:
    _PREFS_DIR.mkdir(parents=True, exist_ok=True)
    _PREFS_FILE.write_text(json.dumps(prefs, indent=2) + "\n")


# Side effects on these paths are expected (e.g. screen dims on battery) — skip
_IGNORE_PATTERNS = (
    "backlight/",     # screen brightness changes with power profile — intentional
    "power_supply/",  # battery status changes — not controllable
)


def save_desired_values(side_effects: dict[str, SideFx]) -> None:
    """Record the 'before' values of detected side effects as desired.

    side_effects: { "leds/tpacpi::kbd_backlight": {"brightness": ("1", "0")} }
    The "1" (before) is what the user wanted; store it.
    """
    prefs = _load()
    for sysfs_key, attrs in side_effects.items():
        if any(sysfs_key.startswith(p) for p in _IGNORE_PATTERNS):
            continue
        for attr, (before_val, _after_val) in attrs.items():
            full_key = f"{sysfs_key}/{attr}"
            prefs[full_key] = before_val
            logger.info("Saved sysfs preference: %s = %s", full_key, before_val)
    _save(prefs)


def restore_desired_values(sudo_manager=None) -> int:
    """Restore all saved sysfs preferences to their desired values.

    Returns the number of values successfully restored.
    """
    prefs = _load()
    if not prefs:
        return 0

    restored = 0
    for sysfs_key, desired in prefs.items():
        sysfs_path = Path("/sys/class") / sysfs_key
        if not sysfs_path.exists():
            continue

        current = None
        try:
            current = sysfs_path.read_text().strip()
        except Exception:
            pass

        if current == desired:
            continue

        ok = _write_sysfs(sysfs_path, desired, sudo_manager)
        if ok:
            restored += 1
            logger.info("Restored sysfs %s → %s", sysfs_key, desired)
        else:
            logger.warning("Failed to restore sysfs %s → %s", sysfs_key, desired)

    return restored


def update_preferences_to_new_values(
    intentional_changes: dict[str, SideFx],
    sudo_manager=None,
) -> None:
    """Update saved preferences to reflect intentional sysfs writes.

    When the user explicitly sets a sysfs value (e.g. "set brightness to 100%"),
    the preference should move to the new value so that future unintended resets
    are still detected and corrected against the right baseline.

    intentional_changes: { "backlight/intel_backlight": {"brightness": ("4819", "19393")} }
    """
    prefs = _load()
    changed = False
    for sysfs_key, attrs in intentional_changes.items():
        if any(sysfs_key.startswith(p) for p in _IGNORE_PATTERNS):
            continue
        for attr, (_old_val, new_val) in attrs.items():
            full_key = f"{sysfs_key}/{attr}"
            if full_key in prefs:
                logger.info("Updated sysfs preference: %s = %s", full_key, new_val)
                prefs[full_key] = new_val
                changed = True
    if changed:
        _save(prefs)


def quick_restore_side_effects(
    side_effects: dict[str, SideFx],
    sudo_manager=None,
) -> list[str]:
    """Restore known side effects without an LLM call.

    If a side effect's 'before' value matches a saved preference, write it
    back immediately.  Returns list of restored paths.
    """
    prefs = _load()
    restored: list[str] = []

    for sysfs_key, attrs in side_effects.items():
        for attr, (before_val, after_val) in attrs.items():
            full_key = f"{sysfs_key}/{attr}"
            desired = prefs.get(full_key)
            if desired is not None and before_val == desired:
                sysfs_path = Path("/sys/class") / full_key
                if _write_sysfs(sysfs_path, desired, sudo_manager):
                    restored.append(full_key)
                    logger.info("Quick-restored %s → %s", full_key, desired)

    return restored


def _write_sysfs(path: Path, value: str, sudo_manager=None) -> bool:
    value = value.strip()
    try:
        path.write_text(value)
        return True
    except PermissionError:
        pass
    except Exception:
        return False

    if sudo_manager:
        try:
            proc = sudo_manager.run_sudo_command(
                ["tee", str(path)], timeout=10, input=value,
            )
            return proc.returncode == 0
        except Exception:
            pass

    try:
        proc = subprocess.run(
            ["sudo", "-n", "tee", str(path)],
            input=value, capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False
