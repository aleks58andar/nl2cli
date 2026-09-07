"""Plan validation and comprehensive safety checks."""

from pathlib import Path

from src.planner import validate_plan_against_host, enhance_plan_with_context
from src.safety import get_safety_checker
from src.schema import Plan, HostFacts, ValidationIssue, EditFileAction, ShellAction


def validate_plan(plan: Plan, host_facts: HostFacts) -> list[ValidationIssue]:
    """
    Comprehensively validate a plan for safety and compatibility.
    
    Args:
        plan: The plan to validate
        host_facts: Information about the host system
    
    Returns:
        List of validation issues found
    """
    issues: list[ValidationIssue] = []
    
    # Basic plan structure validation
    if not plan.actions:
        issues.append(ValidationIssue(
            severity="warning",
            message="Plan contains no actions"
        ))
        return issues
    
    # Host compatibility validation
    host_compatible, host_issues = validate_plan_against_host(plan, host_facts)
    for issue in host_issues:
        issues.append(ValidationIssue(
            severity="error",
            message=f"Host compatibility: {issue}"
        ))
    
    # Safety validation — scope warnings are advisory, not hard blocks
    safety_checker = get_safety_checker()
    plan_safe, safety_issues = safety_checker.check_plan_safety(plan)
    for issue in safety_issues:
        # "consider reducing scope" warnings are informational; the risk gate
        # handles confirmation for elevated-privilege plans
        severity = "warning" if "consider reducing scope" in issue else "error"
        issues.append(ValidationIssue(
            severity=severity,
            message=f"Safety concern: {issue}"
        ))
    
    # Individual action validation
    for i, action in enumerate(plan.actions):
        action_issues = _validate_action(action, host_facts, i)
        issues.extend(action_issues)
    
    # Plan-level validation
    plan_issues = _validate_plan_structure(plan)
    issues.extend(plan_issues)
    
    return issues


def _validate_action(
    action, 
    host_facts: HostFacts, 
    action_index: int
) -> list[ValidationIssue]:
    """Validate an individual action."""
    issues: list[ValidationIssue] = []
    
    if isinstance(action, EditFileAction):
        issues.extend(_validate_edit_file_action(action, host_facts, action_index))
    elif isinstance(action, ShellAction):
        issues.extend(_validate_shell_action(action, host_facts, action_index))
    
    return issues


def _validate_edit_file_action(
    action: EditFileAction, 
    host_facts: HostFacts, 
    action_index: int
) -> list[ValidationIssue]:
    """Validate a file edit action."""
    issues: list[ValidationIssue] = []
    
    # Check if file path is absolute
    path = Path(action.path)
    if not path.is_absolute():
        issues.append(ValidationIssue(
            severity="warning",
            message="File path should be absolute for safety",
            action_index=action_index
        ))
    
    # Check if parent directory exists
    parent_dir = path.parent
    if not parent_dir.exists():
        # Check if this is a standard system directory that should exist
        standard_dirs = ["/usr/local/bin", "/etc", "/home", "/opt", "/var", "/usr/bin", "/usr/sbin"]
        is_standard = any(str(parent_dir).startswith(std_dir) for std_dir in standard_dirs)
        
        if is_standard:
            issues.append(ValidationIssue(
                severity="info",
                message=f"Parent directory {parent_dir} does not exist but is a standard system path - plan should include creating it",
                action_index=action_index
            ))
        else:
            issues.append(ValidationIssue(
                severity="error",
                message=f"Parent directory {parent_dir} does not exist",
                action_index=action_index
            ))
    
    # Check if file exists (for editing)
    if not path.exists():
        issues.append(ValidationIssue(
            severity="info", 
            message=f"File {action.path} does not exist - will be created",
            action_index=action_index
        ))
    
    # Check file permissions
    if path.exists():
        try:
            # Check if file is readable
            path.read_text()
        except PermissionError:
            if not action.requires_sudo:
                issues.append(ValidationIssue(
                    severity="error",
                    message=f"File {action.path} requires elevated permissions but action doesn't specify sudo",
                    action_index=action_index
                ))
        except Exception as e:
            issues.append(ValidationIssue(
                severity="warning",
                message=f"Cannot access file {action.path}: {e}",
                action_index=action_index
            ))
    
    # Validate edit mode parameters
    if action.mode == "replace_line":
        if not action.selector:
            issues.append(ValidationIssue(
                severity="error",
                message=f"replace_line mode requires a selector (regex pattern to match the line to replace). Example: '^\\s*#?\\s*Port\\s+.*' for SSH port",
                action_index=action_index
            ))
        if not action.after:
            issues.append(ValidationIssue(
                severity="error", 
                message="replace_line mode requires 'after' content (the new line to replace with)",
                action_index=action_index
            ))
    elif action.mode == "ensure_kv":
        if not action.ensure_key:
            issues.append(ValidationIssue(
                severity="error",
                message="ensure_kv mode requires ensure_key (the configuration key to set)",
                action_index=action_index
            ))
        if not action.ensure_value:
            issues.append(ValidationIssue(
                severity="error",
                message="ensure_kv mode requires ensure_value (the value to set for the key)",
                action_index=action_index
            ))
    elif action.mode == "insert":
        if not action.after:
            issues.append(ValidationIssue(
                severity="error",
                message="insert mode requires 'after' content (the content to insert)",
                action_index=action_index
            ))
    
    return issues


_SHELL_GLUE_NAMES = frozenset({"bash", "sh", "zsh", "dash", "fish", "csh", "tcsh", "ksh"})


def _validate_shell_action(
    action: ShellAction, 
    host_facts: HostFacts, 
    action_index: int
) -> list[ValidationIssue]:
    """Validate a shell command action."""
    issues: list[ValidationIssue] = []
    
    if not action.argv:
        issues.append(ValidationIssue(
            severity="error",
            message="Shell command has empty argv",
            action_index=action_index
        ))
        return issues

    binary = action.argv[0]
    binary_name = Path(binary).name

    # --- sudo must never appear (belt-and-suspenders; schema also blocks) ---
    if "sudo" in action.argv:
        issues.append(ValidationIssue(
            severity="error",
            message="sudo must not appear in plan commands; use requires_sudo flag",
            action_index=action_index
        ))

    # --- shell glue check (configurable) ---
    try:
        from src.config import get_config
        allow_glue = get_config().safety.allow_shell_glue
    except Exception:
        allow_glue = False

    if not allow_glue:
        if binary_name in _SHELL_GLUE_NAMES and len(action.argv) > 1 and action.argv[1] == "-c":
            issues.append(ValidationIssue(
                severity="error",
                message=f"Explicit shell invocation ({binary_name} -c) is not allowed in PoC",
                action_index=action_index,
            ))
        if binary_name in ("python", "python3") and "-c" in action.argv:
            issues.append(ValidationIssue(
                severity="error",
                message="python -c as shell glue is not allowed in PoC",
                action_index=action_index,
            ))

    # --- binary availability ---
    if binary_name not in host_facts.available_binaries and binary not in host_facts.available_binaries:
        if binary.startswith("/"):
            binary_path = Path(binary)
            parent_exists = any(
                existing.startswith(str(binary_path.parent))
                for existing in host_facts.existing_files
            )
            if not parent_exists:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=f"Binary {binary} path may not exist",
                    action_index=action_index
                ))
        else:
            issues.append(ValidationIssue(
                severity="warning",
                message=f"Binary '{binary}' not found in system PATH",
                action_index=action_index
            ))
    
    return issues


def _validate_plan_structure(plan: Plan) -> list[ValidationIssue]:
    """Validate the overall plan structure and logic."""
    issues: list[ValidationIssue] = []
    
    # Check for excessive sudo usage
    sudo_actions = sum(1 for action in plan.actions if action.requires_sudo)
    if sudo_actions > len(plan.actions) * 0.8:  # More than 80% of actions need sudo
        issues.append(ValidationIssue(
            severity="warning",
            message=f"High number of sudo actions ({sudo_actions}/{len(plan.actions)}) - consider reviewing permissions"
        ))
    
    # Check for risky actions
    risky_actions = sum(1 for action in plan.actions if action.risky)
    if risky_actions > 0:
        issues.append(ValidationIssue(
            severity="warning",
            message=f"Plan contains {risky_actions} action(s) marked as risky"
        ))
    
    # Check for file editing conflicts
    edited_files = {}
    for i, action in enumerate(plan.actions):
        if isinstance(action, EditFileAction):
            if action.path in edited_files:
                issues.append(ValidationIssue(
                    severity="warning", 
                    message=f"File {action.path} is edited multiple times (actions {edited_files[action.path]+1} and {i+1})",
                    action_index=i
                ))
            edited_files[action.path] = i
    
    # Check for service restart patterns
    service_actions = {}
    for i, action in enumerate(plan.actions):
        if action.type == "restart_service":
            service_name = action.service_name
            if service_name in service_actions:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=f"Service {service_name} is restarted multiple times",
                    action_index=i
                ))
            service_actions[service_name] = i
    
    return issues


def enhance_and_validate_plan(plan: Plan, host_facts: HostFacts) -> tuple[Plan, list[ValidationIssue]]:
    """
    Enhance a plan with host context and validate it.
    
    Args:
        plan: The plan to enhance and validate
        host_facts: Information about the host system
    
    Returns:
        Tuple of (enhanced_plan, validation_issues)
    """
    # Enhance the plan with host context
    enhanced_plan = enhance_plan_with_context(plan, host_facts)
    
    # Validate the enhanced plan
    issues = validate_plan(enhanced_plan, host_facts)
    
    return enhanced_plan, issues


def has_critical_issues(issues: list[ValidationIssue]) -> bool:
    """Check if there are any critical (error-level) validation issues."""
    return any(issue.severity == "error" for issue in issues)


def format_validation_issues(issues: list[ValidationIssue]) -> str:
    """Format validation issues for display."""
    if not issues:
        return "No validation issues found."
    
    lines = []
    for issue in issues:
        if issue.severity == "error":
            prefix = "❌"
        elif issue.severity == "warning":
            prefix = "⚠️"
        else:  # info
            prefix = "ℹ️"
        
        action_info = f" (Action {issue.action_index + 1})" if issue.action_index is not None else ""
        lines.append(f"{prefix} {issue.message}{action_info}")
    
    return "\n".join(lines)
