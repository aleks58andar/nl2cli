"""Service management executor for restarting system services."""

import subprocess
from pathlib import Path

from src.core.schema import InitSystem, RestartServiceAction, ShellAction
from src.core.utils import which, run_command
from src.execution.base import ActionRunner, Result
from src.execution.shell import SystemctlRunner, SysVServiceRunner, ServiceRunner


def get_service_runner(action: RestartServiceAction, init_system: InitSystem) -> ActionRunner:
    """Get the appropriate service runner based on the init system."""
    service_action = "restart"

    if init_system == InitSystem.systemd:
        return SystemctlRunner(action.service_name, service_action)
    elif init_system == InitSystem.sysv:
        return SysVServiceRunner(action.service_name, service_action)
    elif init_system == InitSystem.upstart:
        if which("service"):
            return ServiceRunner(action.service_name, service_action)
        else:
            return UpstartServiceRunner(action.service_name, service_action)
    else:
        if which("service"):
            return ServiceRunner(action.service_name, service_action)
        elif which("systemctl"):
            return SystemctlRunner(action.service_name, service_action)
        else:
            return SysVServiceRunner(action.service_name, service_action)


class UpstartServiceRunner(ActionRunner):
    """Runner for Upstart services — uses initctl without shell operators."""

    def __init__(self, service_name: str, action: str = "restart"):
        self.service_name = service_name
        self.upstart_action = action

    @property
    def description(self) -> str:
        return f"{self.upstart_action.title()} {self.service_name} service via Upstart"

    @property
    def requires_sudo(self) -> bool:
        return True

    def run(self, timeout: int = 30) -> Result:
        if not which("initctl"):
            return Result(
                ok=False,
                stderr="Upstart (initctl) is not available on this system",
                error_code=1,
            )

        upstart_conf = Path(f"/etc/init/{self.service_name}.conf")
        alt_conf = Path(f"/etc/init.d/{self.service_name}")
        if not upstart_conf.exists() and not alt_conf.exists():
            return Result(
                ok=False,
                stderr=f"Upstart service '{self.service_name}' configuration not found",
                error_code=1,
            )

        status_check = run_command(
            ["initctl", "status", self.service_name],
            capture_output=True,
            text=True,
        )
        if status_check.returncode != 0 and "Unknown job" in (status_check.stderr or ""):
            return Result(
                ok=False,
                stderr=f"Service '{self.service_name}' is not known to Upstart",
                error_code=1,
            )

        if self.upstart_action == "restart":
            return self._restart_via_stop_start(timeout)

        return self._run_initctl(self.upstart_action, timeout)

    def _run_initctl(self, action: str, timeout: int) -> Result:
        argv = ["sudo", "initctl", action, self.service_name]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return Result(
                ok=proc.returncode == 0,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                changed=True,
                error_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return Result(ok=False, stderr="Timed out", error_code=124)

    def _restart_via_stop_start(self, timeout: int) -> Result:
        """Restart = stop then start (two separate subprocesses)."""
        stop = self._run_initctl("stop", timeout)
        if not stop.ok:
            return stop
        return self._run_initctl("start", timeout)

    def dry_run_description(self) -> str:
        return f"Would execute: sudo initctl {self.upstart_action} {self.service_name}"
