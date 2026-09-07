"""Running a validated Plan against the real system.

Every runner takes argv lists and runs them with shell=False; privilege is
applied here by SudoManager, never carried in a command string.
"""

from src.execution.base import ActionRunner, ExecutionError, Result
from src.execution.filesystem import EditFileRunner
from src.execution.runner import get_action_runner, run_plan
from src.execution.services import get_service_runner
from src.execution.shell import ShellRunner
from src.execution.sudo_manager import SudoManager

__all__ = [
    "ActionRunner",
    "EditFileRunner",
    "ExecutionError",
    "Result",
    "ShellRunner",
    "SudoManager",
    "get_action_runner",
    "get_service_runner",
    "run_plan",
]
