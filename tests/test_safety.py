"""Tests for safety checking."""

import pytest

from src.safety import SafetyChecker, is_command_safe, get_dangerous_command_reason
from src.schema import ShellAction, EditFileAction, EditMode


def test_safety_checker_init():
    """Test SafetyChecker initialization."""
    checker = SafetyChecker()
    assert checker.config is not None
    assert len(checker.dangerous_regexes) > 0


def test_dangerous_commands():
    """Test detection of dangerous commands."""
    checker = SafetyChecker()
    
    dangerous_commands = [
        "rm -rf /",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        ":(){:|:&};:",  # fork bomb
        "shutdown -h now"
    ]
    
    for cmd in dangerous_commands:
        is_safe, issues = checker.check_command_safety(cmd)
        assert not is_safe, f"Command should be unsafe: {cmd}"
        assert len(issues) > 0


def test_safe_commands():
    """Test that safe commands pass validation."""
    checker = SafetyChecker()
    
    safe_commands = [
        "systemctl restart nginx",
        "apt update",
        "ls -la",
        "grep pattern file.txt",
        "cp file.txt backup.txt"
    ]
    
    for cmd in safe_commands:
        is_safe, issues = checker.check_command_safety(cmd)
        assert is_safe, f"Command should be safe: {cmd} (issues: {issues})"


def test_shell_action_safety():
    """Test safety checking for shell actions."""
    checker = SafetyChecker()
    
    safe_action = ShellAction(
        description="List files",
        command="ls -la"
    )
    
    is_safe, issues = checker.check_action_safety(safe_action)
    assert is_safe
    
    dangerous_action = ShellAction(
        description="Dangerous command",
        command="rm -rf /"
    )
    
    is_safe, issues = checker.check_action_safety(dangerous_action)
    assert not is_safe
    assert len(issues) > 0


def test_file_safety():
    """Test file operation safety checks."""
    checker = SafetyChecker()
    
    # Safe file
    is_safe, issues = checker.check_file_safety("/etc/nginx/nginx.conf", for_writing=True)
    assert is_safe or len(issues) == 0  # Might warn but not fail
    
    # Critical system file
    is_safe, issues = checker.check_file_safety("/etc/passwd", for_writing=True)
    assert not is_safe
    assert len(issues) > 0


def test_convenience_functions():
    """Test convenience functions."""
    assert is_command_safe("ls -la")
    assert not is_command_safe("rm -rf /")
    
    reason = get_dangerous_command_reason("rm -rf /")
    assert reason is not None
    assert "recursive deletion" in reason.lower() or "dangerous" in reason.lower()
    
    safe_reason = get_dangerous_command_reason("ls -la")
    assert safe_reason is None
