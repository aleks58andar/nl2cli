"""Safety checks and validation for commands and actions."""

import re
import shlex
from pathlib import Path

from src.config import get_config
from src.schema import Action, Plan, ShellAction, EditFileAction, RestartServiceAction


class SafetyError(Exception):
    """Raised when a safety check fails."""


class SafetyChecker:
    """Performs safety checks on commands and plans."""
    
    def __init__(self) -> None:
        from src.config import get_default_safety_config
        try:
            self.config = get_config().safety
            # If config has no patterns, use defaults
            if not self.config.dangerous_patterns:
                default_config = get_default_safety_config()
                self.config.dangerous_patterns = default_config.dangerous_patterns
        except Exception:
            self.config = get_default_safety_config()
        self._compile_patterns()
    
    def _compile_patterns(self) -> None:
        """Compile regex patterns for efficiency."""
        self.dangerous_regexes = [
            re.compile(pattern, re.IGNORECASE) 
            for pattern in self.config.dangerous_patterns
        ]
    
    def check_command_safety(self, command: str) -> tuple[bool, list[str]]:
        """
        Check if a command is safe to execute.
        
        Returns:
            (is_safe, list_of_issues)
        """
        issues = []
        
        # Check against dangerous patterns
        for pattern in self.dangerous_regexes:
            if pattern.search(command):
                issues.append(f"Command matches dangerous pattern: {pattern.pattern}")
        
        # Parse command to check binary
        try:
            tokens = shlex.split(command)
            if tokens:
                binary = tokens[0]
                
                # Remove sudo prefix if present
                if binary == "sudo" and len(tokens) > 1:
                    binary = tokens[1]
                
                # Check against denied binaries
                if binary in self.config.denied_binaries:
                    issues.append(f"Binary '{binary}' is explicitly denied")
                
                # Check if binary is in allowed list (if list is not empty)
                if (self.config.allowed_binaries and 
                    binary not in self.config.allowed_binaries and
                    not binary.startswith("/etc/init.d/")):  # Allow init.d scripts
                    issues.append(f"Binary '{binary}' is not in allowed list")
        
        except ValueError as e:
            issues.append(f"Could not parse command: {e}")
        
        # Additional heuristic checks
        issues.extend(self._heuristic_checks(command))
        
        return len(issues) == 0, issues
    
    def _heuristic_checks(self, command: str) -> list[str]:
        """Perform additional heuristic safety checks."""
        issues = []
        
        # Check for suspicious combinations
        if "rm" in command and "/" in command:
            if re.search(r"rm.*-r.*[/\\]", command, re.IGNORECASE):
                issues.append("Recursive deletion detected")
        
        # Check for privilege escalation attempts
        if re.search(r"sudo.*sudo", command, re.IGNORECASE):
            issues.append("Multiple sudo invocations detected")
        
        # Check for shell injection patterns
        suspicious_chars = [";", "|", "&", "`", "$"]
        if any(char in command for char in suspicious_chars):
            # Allow some safe uses
            if not re.search(r"systemctl.*\|\|", command):  # Allow systemctl restart || true
                issues.append("Command contains shell metacharacters that could be dangerous")
        
        # Check for network operations without explicit permission
        network_commands = ["wget", "curl", "nc", "netcat", "telnet", "ssh", "scp", "rsync"]
        for net_cmd in network_commands:
            if re.search(rf"\b{net_cmd}\b", command, re.IGNORECASE):
                issues.append(f"Network operation detected: {net_cmd}")
        
        return issues
    
    def check_file_safety(self, file_path: str, for_writing: bool = True) -> tuple[bool, list[str]]:
        """
        Check if a file operation is safe.
        
        Args:
            file_path: Path to the file
            for_writing: Whether the file will be written to
        """
        issues = []
        path = Path(file_path)
        
        # Critical system files that should never be modified
        critical_files = {
            "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow",
            "/etc/sudoers", "/boot/grub/grub.cfg", "/boot/grub2/grub.cfg",
            "/etc/fstab", "/etc/hosts.allow", "/etc/hosts.deny",
            "/proc/sys/kernel/", "/dev/",
            # Dangerous sysfs subtrees (kernel internals, firmware, raw devices)
            "/sys/kernel/", "/sys/firmware/", "/sys/bus/",
        }
        # sysfs hardware control paths that are legitimate to write
        sysfs_allowed_prefixes = (
            "/sys/class/leds/",
            "/sys/class/backlight/",
            "/sys/class/power_supply/",
        )

        p_str = str(path)
        if any(p_str.startswith(a) for a in sysfs_allowed_prefixes):
            pass  # explicitly allowed — hardware control via sysfs
        else:
            # Check against critical files
            for critical in critical_files:
                if p_str.startswith(critical):
                    if for_writing:
                        issues.append(f"Attempting to modify critical system file: {file_path}")
                    else:
                        issues.append(f"Reading sensitive system file: {file_path}")
        
        # Check file size for reads
        if not for_writing and path.exists():
            try:
                size_mb = path.stat().st_size / (1024 * 1024)
                if size_mb > self.config.max_file_size_mb:
                    issues.append(f"File is too large: {size_mb:.1f}MB > {self.config.max_file_size_mb}MB")
            except OSError:
                issues.append(f"Cannot access file: {file_path}")
        
        # Check for suspicious paths
        if ".." in str(path):
            issues.append("Path contains directory traversal (..) - potential security risk")
        
        return len(issues) == 0, issues
    
    def check_action_safety(self, action: Action) -> tuple[bool, list[str]]:
        """Check if an individual action is safe."""
        issues = []
        
        if isinstance(action, ShellAction):
            is_safe, cmd_issues = self.check_command_safety(action.command)
            issues.extend(cmd_issues)
        
        elif isinstance(action, EditFileAction):
            is_safe, file_issues = self.check_file_safety(action.path, for_writing=True)
            issues.extend(file_issues)
        
        elif isinstance(action, RestartServiceAction):
            # Service names should be alphanumeric with limited special chars
            if not re.match(r"^[a-zA-Z0-9._-]+$", action.service_name):
                issues.append(f"Service name contains suspicious characters: {action.service_name}")
        
        # Check if action is marked as risky by LLM
        if action.risky:
            issues.append("Action is marked as risky by the AI model")
        
        return len(issues) == 0, issues
    
    def check_plan_safety(self, plan: Plan) -> tuple[bool, list[str]]:
        """Check if an entire plan is safe to execute."""
        all_issues = []
        
        for i, action in enumerate(plan.actions):
            is_safe, issues = self.check_action_safety(action)
            for issue in issues:
                all_issues.append(f"Action {i + 1}: {issue}")
        
        # Warn (not block) when many actions need sudo — legitimate multi-step
        # tasks routinely need this (e.g. configure system at boot requires
        # writing several files + daemon-reload + enable units).
        sudo_actions = sum(1 for action in plan.actions if action.requires_sudo)
        if sudo_actions > 10:
            all_issues.append(f"Plan requires sudo for {sudo_actions} actions - consider reducing scope")
        
        # Check for file conflicts (editing same file multiple times)
        edited_files: set[str] = set()
        for i, action in enumerate(plan.actions):
            if isinstance(action, EditFileAction):
                if action.path in edited_files:
                    all_issues.append(f"Action {i + 1}: File {action.path} is edited multiple times")
                edited_files.add(action.path)
        
        return len(all_issues) == 0, all_issues


def get_safety_checker() -> SafetyChecker:
    """Get a configured safety checker instance."""
    return SafetyChecker()


def is_command_safe(command: str) -> bool:
    """Quick check if a command is safe (simplified interface)."""
    checker = get_safety_checker()
    is_safe, _ = checker.check_command_safety(command)
    return is_safe


def get_dangerous_command_reason(command: str) -> str | None:
    """Get the reason why a command is considered dangerous."""
    checker = get_safety_checker()
    is_safe, issues = checker.check_command_safety(command)
    if not is_safe and issues:
        return issues[0]  # Return first issue
    return None
