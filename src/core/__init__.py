"""Domain types and host facts — the layer everything else builds on.

Nothing here imports from another nl2cli package: `core` is the bottom of
the dependency graph.
"""

from src.core.config import AppConfig, get_config, load_config
from src.core.schema import (
    Action,
    ActionType,
    EditFileAction,
    EditMode,
    HostFacts,
    InitSystem,
    Plan,
    RestartServiceAction,
    ShellAction,
    ValidationIssue,
)
from src.core.state_diff import diff_snapshots, format_side_effects, take_snapshot
from src.core.utils import gather_host_facts, run_command, which

__all__ = [
    "Action",
    "ActionType",
    "AppConfig",
    "EditFileAction",
    "EditMode",
    "HostFacts",
    "InitSystem",
    "Plan",
    "RestartServiceAction",
    "ShellAction",
    "ValidationIssue",
    "diff_snapshots",
    "format_side_effects",
    "gather_host_facts",
    "get_config",
    "load_config",
    "run_command",
    "take_snapshot",
    "which",
]
