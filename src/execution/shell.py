"""Shell command executor — always uses argv lists, never shell=True."""

import os
import shlex
import signal
import subprocess

from rich.console import Console

from src.core.schema import ShellAction
from src.core.utils import run_command
from src.execution.base import ActionRunner, Result

console = Console()


class ShellRunner(ActionRunner):
    """Executes shell commands via argv lists with proper cleanup."""

    def __init__(self, action: ShellAction, sudo_manager=None):
        self.action = action
        self.sudo_manager = sudo_manager

    @classmethod
    def from_command(
        cls,
        description: str,
        command: str,
        requires_sudo: bool = False,
        risky: bool = False,
    ) -> "ShellRunner":
        action = ShellAction(
            description=description,
            command=command,
            requires_sudo=requires_sudo,
            risky=risky,
        )
        return cls(action)

    @property
    def description(self) -> str:
        return self.action.description

    @property
    def requires_sudo(self) -> bool:
        return self.action.requires_sudo

    # ------------------------------------------------------------------

    def _prepare_argv(self) -> list[str]:
        """Return the raw argv (without sudo)."""
        return list(self.action.argv)

    def run(self, timeout: int = 30) -> Result:
        argv = self._prepare_argv()

        try:
            if self.action.requires_sudo and self.sudo_manager:
                proc = self.sudo_manager.run_sudo_command(argv, timeout=timeout)
            elif self.action.requires_sudo:
                proc = subprocess.run(
                    ["sudo"] + argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            elif getattr(self.action, "uses_pipe", False):
                # Read-only piped command — run through bash with shell=True.
                # argv is already ["bash", "-c", "the command"].
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    shell=False,  # bash -c handles the shell interpretation
                )
            else:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )

            # Cap output to avoid token explosion in downstream LLM calls
            # (e.g. bare `du` on a system recurses into /proc and generates MBs).
            _MAX_STDOUT = 8_000
            _MAX_STDERR = 2_000
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            if len(stdout) > _MAX_STDOUT:
                stdout = stdout[:_MAX_STDOUT] + f"\n[...output truncated at {_MAX_STDOUT} chars]"
            if len(stderr) > _MAX_STDERR:
                stderr = stderr[:_MAX_STDERR] + f"\n[...stderr truncated at {_MAX_STDERR} chars]"

            return Result(
                ok=proc.returncode == 0,
                stdout=stdout,
                stderr=stderr,
                changed=True,
                error_code=proc.returncode,
            )

        except subprocess.TimeoutExpired:
            return Result(
                ok=False,
                stderr=f"Command timed out after {timeout} seconds.",
                error_code=124,
            )
        except Exception as e:
            return Result(
                ok=False,
                stderr=f"Failed to execute command: {e}",
                error_code=1,
            )

    def dry_run_description(self) -> str:
        argv = self._prepare_argv()
        if self.action.requires_sudo:
            argv = ["sudo"] + argv
        return f"Would execute: {shlex.join(argv)}"


# ------------------------------------------------------------------
# Service-oriented runners (construct safe argv internally)
# ------------------------------------------------------------------


class SystemctlRunner(ShellRunner):
    """Specialized runner for systemctl commands."""

    def __init__(self, service_name: str, action: str = "restart"):
        command = f"systemctl {action} {service_name}"
        description = f"{action.title()} {service_name} service via systemd"

        super().__init__(
            ShellAction(
                description=description,
                command=command,
                requires_sudo=True,
            )
        )
        self.service_name = service_name
        self.systemctl_action = action

    def run(self, timeout: int = 30) -> Result:
        version_check = run_command(
            ["systemctl", "--version"], capture_output=True, text=True
        )
        if version_check.returncode != 0:
            return Result(
                ok=False,
                stderr="systemd is not available on this system",
                error_code=1,
            )

        resolved = self._resolve_service_name()
        if resolved is None:
            return Result(
                ok=False,
                stderr=f"Service '{self.service_name}' not found",
                error_code=1,
            )

        if resolved != self.service_name:
            self.service_name = resolved
            command = f"systemctl {self.systemctl_action} {resolved}"
            self.action = ShellAction(
                description=self.action.description,
                command=command,
                requires_sudo=self.action.requires_sudo,
            )

        return super().run(timeout=timeout)

    def _resolve_service_name(self) -> str | None:
        """Try variations of the service name until systemctl finds one."""
        candidates = [self.service_name]
        if not self.service_name.endswith(".service"):
            candidates.append(f"{self.service_name}.service")

        for name in candidates:
            check = run_command(
                ["systemctl", "show", "-p", "LoadState", name],
                capture_output=True, text=True,
            )
            if check.returncode == 0 and "LoadState=loaded" in (check.stdout or ""):
                return name
        return None


class SysVServiceRunner(ShellRunner):
    """Specialized runner for SysV init scripts."""

    def __init__(self, service_name: str, action: str = "restart"):
        command = f"/etc/init.d/{service_name} {action}"
        description = f"{action.title()} {service_name} service via SysV init"

        super().__init__(
            ShellAction(
                description=description,
                command=command,
                requires_sudo=True,
            )
        )
        self.service_name = service_name
        self.sysv_action = action

    def run(self, timeout: int = 30) -> Result:
        from pathlib import Path

        init_script = Path(f"/etc/init.d/{self.service_name}")
        if not init_script.exists():
            return Result(
                ok=False,
                stderr=f"Init script '/etc/init.d/{self.service_name}' not found",
                error_code=1,
            )
        if not init_script.is_file():
            return Result(
                ok=False,
                stderr=f"'/etc/init.d/{self.service_name}' is not a file",
                error_code=1,
            )
        return super().run(timeout=timeout)


class ServiceRunner(ShellRunner):
    """Specialized runner for the 'service' command."""

    def __init__(self, service_name: str, action: str = "restart"):
        command = f"service {service_name} {action}"
        description = f"{action.title()} {service_name} service via service command"

        super().__init__(
            ShellAction(
                description=description,
                command=command,
                requires_sudo=True,
            )
        )
        self.service_name = service_name
        self.service_action = action
