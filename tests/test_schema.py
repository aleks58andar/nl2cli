"""Tests for schema models."""

import pytest
from pydantic import ValidationError

from src.core.schema import (
    EditFileAction, ShellAction, RestartServiceAction, 
    Plan, HostFacts, InitSystem, EditMode
)


def test_edit_file_action():
    """Test EditFileAction creation and validation."""
    action = EditFileAction(
        description="Edit config file",
        path="/etc/config.conf",
        mode=EditMode.replace_line,
        selector="old_value",
        after="new_value"
    )
    
    assert action.type == "edit_file"
    assert action.description == "Edit config file"
    assert action.path == "/etc/config.conf"
    assert action.backup is True  # default value
    assert not action.requires_sudo  # default value


def test_shell_action():
    """Test ShellAction creation and validation."""
    action = ShellAction(
        description="List files",
        command="ls -la",
        requires_sudo=False
    )
    
    assert action.type == "shell"
    assert action.command == "ls -la"
    assert not action.requires_sudo


def test_restart_service_action():
    """Test RestartServiceAction creation."""
    action = RestartServiceAction(
        description="Restart nginx",
        service_name="nginx",
        requires_sudo=True
    )
    
    assert action.type == "restart_service"
    assert action.service_name == "nginx"
    assert action.requires_sudo


def test_plan_creation():
    """Test Plan creation with multiple actions."""
    edit_action = EditFileAction(
        description="Edit config",
        path="/etc/test.conf",
        mode=EditMode.ensure_kv,
        ensure_key="setting",
        ensure_value="value"
    )
    
    shell_action = ShellAction(
        description="Run command",
        command="echo test"
    )
    
    plan = Plan(
        user_request="Configure test setting",
        actions=[edit_action, shell_action],
        init_system=InitSystem.systemd
    )
    
    assert plan.user_request == "Configure test setting"
    assert len(plan.actions) == 2
    assert plan.init_system == InitSystem.systemd


def test_host_facts():
    """Test HostFacts model."""
    facts = HostFacts(
        distro_id="ubuntu",
        distro_version="22.04",
        init_system=InitSystem.systemd,
        available_binaries=["systemctl", "apt"],
        existing_files=["/etc/systemd", "/etc/apt"],
        python_version="3.11.0"
    )
    
    assert facts.distro_id == "ubuntu"
    assert facts.init_system == InitSystem.systemd
    assert "systemctl" in facts.available_binaries


def test_invalid_edit_mode():
    """Test that invalid edit mode raises validation error."""
    with pytest.raises(ValidationError):
        EditFileAction(
            description="Test",
            path="/test",
            mode="invalid_mode"  # This should fail
        )
