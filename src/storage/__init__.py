"""Everything nl2cli persists between runs.

The audit log records what happened; sysfs preferences record what the user
wanted, so an unintended hardware side effect can be corrected next time.
"""

from src.storage.audit import format_action_result, write_audit_entry
from src.storage.sysfs_prefs import (
    quick_restore_side_effects,
    restore_desired_values,
    save_desired_values,
    update_preferences_to_new_values,
)

__all__ = [
    "format_action_result",
    "quick_restore_side_effects",
    "restore_desired_values",
    "save_desired_values",
    "update_preferences_to_new_values",
    "write_audit_entry",
]
