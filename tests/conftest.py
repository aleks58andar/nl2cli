"""Shared fixtures for the nl2cli test suite."""

import pytest

from src.schema import (
    ActionType,
    EditFileAction,
    EditMode,
    HostFacts,
    InitSystem,
    Plan,
    RestartServiceAction,
    ShellAction,
)


@pytest.fixture
def host_facts() -> HostFacts:
    """A systemd host with a small, predictable set of binaries."""
    return HostFacts(
        distro_id="pop",
        distro_version="22.04",
        init_system=InitSystem.systemd,
        available_binaries=["systemctl", "ls", "echo", "cat", "grep", "apt"],
        existing_files=["/etc", "/etc/ssh", "/usr/bin", "/var/log"],
        kernel_version="6.12.10",
        python_version="3.10.12",
        machine_vendor="System76",
        machine_model="Gazelle",
        is_laptop=True,
        active_services=["ssh.service"],
        package_manager="apt",
    )


@pytest.fixture
def sysv_host_facts(host_facts: HostFacts) -> HostFacts:
    """The same host, but running SysV init."""
    facts = host_facts.model_copy(deep=True)
    facts.init_system = InitSystem.sysv
    facts.available_binaries = ["service", "ls", "echo"]
    return facts


def make_plan(*actions, request: str = "test request", **kwargs) -> Plan:
    """Build a Plan around the given actions."""
    return Plan(user_request=request, actions=list(actions), **kwargs)


def shell(command: str, *, sudo: bool = False, risky: bool = False, **kwargs) -> ShellAction:
    """Build a ShellAction from a command string."""
    return ShellAction(
        description=kwargs.pop("description", command),
        command=command,
        requires_sudo=sudo,
        risky=risky,
        **kwargs,
    )


def edit(path: str, *, mode: EditMode = EditMode.ensure_kv, **kwargs) -> EditFileAction:
    """Build an EditFileAction with sensible defaults for the mode."""
    defaults: dict = {}
    if mode == EditMode.ensure_kv:
        defaults = {"ensure_key": "Key", "ensure_value": "value"}
    elif mode == EditMode.replace_line:
        defaults = {"selector": "^Key ", "after": "Key value"}
    elif mode == EditMode.insert:
        defaults = {"after": "new line"}
    defaults.update(kwargs)
    description = defaults.pop("description", f"edit {path}")
    requires_sudo = defaults.pop("requires_sudo", False)
    return EditFileAction(
        description=description,
        path=path,
        mode=mode,
        requires_sudo=requires_sudo,
        **defaults,
    )


def restart(service: str, **kwargs) -> RestartServiceAction:
    """Build a RestartServiceAction."""
    return RestartServiceAction(
        description=kwargs.pop("description", f"restart {service}"),
        service_name=service,
        **kwargs,
    )


__all__ = [
    "ActionType",
    "EditMode",
    "InitSystem",
    "edit",
    "host_facts",
    "make_plan",
    "restart",
    "shell",
    "sysv_host_facts",
]
