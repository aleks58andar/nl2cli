"""Pydantic models for nl2cli structured plans and actions."""

import shlex
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# Shell characters/patterns that are always forbidden (write/chain/injection risk).
_FORBIDDEN_SHELL_CHARS = set(";&<>`")
_FORBIDDEN_SHELL_PATTERNS = ("$(", ">>", " > ", "||", "&&")

# | is allowed in read-only (non-sudo) commands — checked separately.
# These patterns inside piped commands indicate write intent and are blocked.
_PIPE_WRITE_PATTERNS = ("tee ", " > ", ">>", " dd ", "chmod ", "chown ")


class InitSystem(str, Enum):
    """Detected init system type."""
    
    systemd = "systemd"
    sysv = "sysv"
    upstart = "upstart"
    unknown = "unknown"


class ActionType(str, Enum):
    """Types of actions that can be performed."""
    
    edit_file = "edit_file"
    shell = "shell"
    restart_service = "restart_service"


class EditMode(str, Enum):
    """File editing modes."""
    
    replace_line = "replace_line"
    insert = "insert"
    ensure_kv = "ensure_kv"  # for INI/TOML config files


class ActionBase(BaseModel):
    """Base class for all actions."""
    
    type: ActionType
    description: str
    requires_sudo: bool = False
    risky: bool = False  # heuristic flag from LLM + local review
    rollback_hint: str | None = None


class EditFileAction(ActionBase):
    """Action to edit a file on the filesystem."""
    
    type: Literal[ActionType.edit_file] = ActionType.edit_file
    path: str
    backup: bool = True
    mode: EditMode
    selector: str | None = None  # e.g., line startswith or regex
    before: str | None = None
    after: str | None = None
    ensure_key: str | None = None
    ensure_value: str | None = None


class ShellAction(ActionBase):
    """Action to execute a shell command."""
    
    type: Literal[ActionType.shell] = ActionType.shell
    command: str
    argv: list[str] = Field(default_factory=list)

    # Set by the validator when a pipe is present — execution uses shell=True
    uses_pipe: bool = False

    @model_validator(mode="after")
    def _validate_and_build_argv(self) -> "ShellAction":
        cmd = self.command.strip()
        if not cmd and not self.argv:
            raise ValueError("ShellAction requires 'command' or 'argv'")

        if cmd:
            for ch in _FORBIDDEN_SHELL_CHARS:
                if ch in cmd:
                    raise ValueError(
                        f"Shell operator '{ch}' is not allowed in commands"
                    )
            for pat in _FORBIDDEN_SHELL_PATTERNS:
                if pat in cmd:
                    raise ValueError(
                        f"Shell pattern '{pat}' is not allowed in commands"
                    )

            if "|" in cmd:
                if self.requires_sudo:
                    raise ValueError(
                        "Pipes are not allowed in sudo commands — split into separate actions"
                    )
                for pat in _PIPE_WRITE_PATTERNS:
                    if pat in cmd:
                        raise ValueError(
                            f"Pipe with write operation '{pat.strip()}' is not allowed"
                        )
                # Read-only pipe: store whole command, mark for shell execution
                self.uses_pipe = True
                self.argv = ["bash", "-c", cmd]
            else:
                self.argv = shlex.split(cmd)

        if not self.argv:
            raise ValueError("ShellAction resulted in empty argv")

        if "sudo" in self.argv:
            raise ValueError(
                "sudo must not appear in plan commands; set requires_sudo=True instead"
            )

        return self


class RestartServiceAction(ActionBase):
    """Action to restart a system service."""
    
    type: Literal[ActionType.restart_service] = ActionType.restart_service
    service_name: str


# Union type for all possible actions with discriminated union
Action = Annotated[EditFileAction | ShellAction | RestartServiceAction, Field(discriminator='type')]


class Plan(BaseModel):
    """Complete structured plan for executing a natural language request."""
    
    user_request: str
    distro_hint: str | None = None
    init_system: InitSystem = InitSystem.unknown
    actions: list[Action] = Field(default_factory=list)
    notes: str | None = None

    model_config = {"use_enum_values": True, "validate_assignment": True}


class ValidationIssue(BaseModel):
    """Represents a validation issue found in a plan."""
    
    severity: Literal["error", "warning", "info"] = "error"
    message: str
    action_index: int | None = None  # None for plan-level issues


class HostFacts(BaseModel):
    """Information about the host system."""
    
    distro_id: str
    distro_version: str
    init_system: InitSystem
    available_binaries: list[str] = Field(default_factory=list)
    existing_files: list[str] = Field(default_factory=list)
    kernel_version: str | None = None
    python_version: str

    # Hardware identity
    machine_vendor: str | None = None   # e.g. "System76"
    machine_model: str | None = None    # e.g. "Lemur Pro"
    is_laptop: bool = False

    # Active systemd services relevant to system management
    active_services: list[str] = Field(default_factory=list)

    # Package manager available on this system
    package_manager: str | None = None

    # Store enums as their string values, like Plan does. Without this,
    # init_system interpolates as "InitSystem.systemd" on Python 3.11+
    # (3.11 changed __format__ for mixin enums) — which leaks into the
    # LLM prompt and into user-facing messages. validate_assignment keeps
    # that true for fields set after construction, not just at parse time.
    model_config = {"use_enum_values": True, "validate_assignment": True}
