"""Host compatibility checks and plan enrichment.

Answers "can this plan run on *this* machine?" — init system, required
binaries, parent directories — and fills in host details the LLM left blank.

Lived in the planner originally, which inverted the layering: validation
imported from planning to reach it. It belongs here.
"""

import os

from src.core.schema import HostFacts, Plan

_VIRTUAL_FS_PREFIXES = ("/sys/", "/proc/", "/dev/")


def validate_plan_against_host(plan: Plan, host_facts: HostFacts) -> tuple[bool, list[str]]:
    """
    Validate that a plan is compatible with the host system.
    
    Args:
        plan: The plan to validate
        host_facts: Information about the host system
    
    Returns:
        Tuple of (is_valid, list_of_issues)
    """
    issues = []
    
    # Check if the plan's init system matches the host
    if plan.init_system != host_facts.init_system and plan.init_system != "unknown":
        issues.append(
            f"Plan expects {plan.init_system} but host uses {host_facts.init_system}"
        )
    
    # Check for required binaries
    required_binaries = set()
    for action in plan.actions:
        if action.type == "shell":
            if action.argv:
                required_binaries.add(action.argv[0])
        elif action.type == "restart_service":
            # Check for service management tools
            if host_facts.init_system == "systemd":
                required_binaries.add("systemctl")
            elif host_facts.init_system == "sysv":
                required_binaries.add("service")
    
    # Check if required binaries are available
    for binary in required_binaries:
        if binary not in host_facts.available_binaries:
            issues.append(f"Required binary '{binary}' not found on system")
    
    # Check for file dependencies
    for action in plan.actions:
        if action.type == "edit_file":
            file_path = action.path
            # Virtual filesystems always exist — skip the parent-dir check
            if any(file_path.startswith(p) for p in _VIRTUAL_FS_PREFIXES):
                continue
            parent_dir = os.path.dirname(file_path)
            if parent_dir:
                if parent_dir in host_facts.existing_files:
                    continue
                parent_exists = any(
                    existing == parent_dir or existing.startswith(parent_dir + "/") or parent_dir.startswith(existing + "/")
                    for existing in host_facts.existing_files
                )
                if not parent_exists:
                    issues.append(f"Parent directory for {file_path} may not exist")
    
    return len(issues) == 0, issues


def enhance_plan_with_context(plan: Plan, host_facts: HostFacts) -> Plan:
    """
    Enhance a plan with additional context from the host system.
    
    Args:
        plan: The plan to enhance
        host_facts: Information about the host system
    
    Returns:
        Enhanced plan
    """
    # Update init system if it was unknown
    if plan.init_system == "unknown":
        plan.init_system = host_facts.init_system
    
    # Add distro hint if missing
    if not plan.distro_hint:
        plan.distro_hint = f"{host_facts.distro_id} {host_facts.distro_version}"
    
    # Enhance actions with system-specific details
    for action in plan.actions:
        if action.type == "restart_service":
            # Ensure the action is compatible with the init system
            if host_facts.init_system == "systemd":
                if "systemctl" not in action.description:
                    action.description = f"Restart {action.service_name} service via systemd"
            elif host_facts.init_system == "sysv":
                if "init.d" not in action.description:
                    action.description = f"Restart {action.service_name} service via SysV init"
    
    return plan
