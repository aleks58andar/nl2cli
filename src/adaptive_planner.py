"""Adaptive planning that can handle missing tools and alternative approaches."""

import logging

from src.model_client import create_plan_generation_request, call_llm_structured, call_llm_with_lookups
from src.planner import PlannerError
from src.schema import Plan, HostFacts, ShellAction, ActionType
from src.utils import which


logger = logging.getLogger(__name__)


class ToolAlternatives:
    """Maps missing tools to alternatives or installation commands."""
    
    # Common tool alternatives
    ALTERNATIVES = {
        "apt-get": ["apt", "dnf", "yum", "pacman", "zypper"],
        "apt": ["apt-get", "dnf", "yum", "pacman", "zypper"],
        "yum": ["dnf", "apt", "apt-get", "pacman", "zypper"],
        "dnf": ["yum", "apt", "apt-get", "pacman", "zypper"],
        "pacman": ["apt", "apt-get", "dnf", "yum", "zypper"],
        "zypper": ["apt", "apt-get", "dnf", "yum", "pacman"],
        "systemctl": ["service", "initctl"],
        "service": ["systemctl"],
        "busctl": ["dbus-send", "gdbus"],
        "nmcli": ["netplan", "iwconfig", "ifconfig"],
        "curl": ["wget"],
        "wget": ["curl"],
        "git": [],
        "docker": [],
        "snap": ["flatpak", "appimage"],
        "flatpak": ["snap", "appimage"],
        # Power management — ordered by preference on modern systems
        "cpupower": ["system76-power", "powerprofilesctl", "tuned-adm", "tlp", "cpufreq-set"],
        "cpufreq-set": ["system76-power", "powerprofilesctl", "cpupower", "tuned-adm"],
        "powerprofilesctl": ["system76-power", "tuned-adm", "cpupower", "tlp"],
        "tuned-adm": ["system76-power", "powerprofilesctl", "cpupower", "tlp"],
        "tlp": ["system76-power", "powerprofilesctl", "tuned-adm"],
    }
    
    # Tools that can be auto-installed
    INSTALLABLE_TOOLS = {
        "curl": {
            "apt": "curl",
            "dnf": "curl", 
            "yum": "curl",
            "pacman": "curl",
            "zypper": "curl"
        },
        "wget": {
            "apt": "wget",
            "dnf": "wget",
            "yum": "wget", 
            "pacman": "wget",
            "zypper": "wget"
        },
        "git": {
            "apt": "git",
            "dnf": "git",
            "yum": "git",
            "pacman": "git", 
            "zypper": "git"
        },
        "busctl": {
            "apt": "systemd",
            "dnf": "systemd",
            "yum": "systemd",
            "pacman": "systemd",
            "zypper": "systemd"
        },
        "dbus-send": {
            "apt": "dbus",
            "dnf": "dbus",
            "yum": "dbus",
            "pacman": "dbus",
            "zypper": "dbus"
        },
        "nmcli": {
            "apt": "network-manager",
            "dnf": "NetworkManager", 
            "yum": "NetworkManager",
            "pacman": "networkmanager",
            "zypper": "NetworkManager"
        }
    }
    
    @classmethod
    def get_alternatives(cls, tool: str) -> list[str]:
        """Get alternative tools for a missing tool."""
        return cls.ALTERNATIVES.get(tool, [])
    
    @classmethod
    def can_install(cls, tool: str) -> bool:
        """Check if a tool can be auto-installed."""
        return tool in cls.INSTALLABLE_TOOLS
    
    @classmethod
    def get_install_package(cls, tool: str, package_manager: str) -> str | None:
        """Get the package name to install for a tool."""
        if tool not in cls.INSTALLABLE_TOOLS:
            return None
        return cls.INSTALLABLE_TOOLS[tool].get(package_manager)


def detect_missing_tools(plan: Plan, host_facts: HostFacts) -> set[str]:
    """Detect missing tools required by the plan."""
    missing_tools = set()
    
    for action in plan.actions:
        if action.type == "shell":
            if action.argv:
                binary = action.argv[0]
                if binary not in host_facts.available_binaries:
                    missing_tools.add(binary)
        
        elif action.type == "restart_service":
            # Check for service management tools
            if host_facts.init_system == "systemd":
                if "systemctl" not in host_facts.available_binaries:
                    missing_tools.add("systemctl")
            elif host_facts.init_system == "sysv":
                if "service" not in host_facts.available_binaries:
                    missing_tools.add("service")
    
    return missing_tools


def find_working_alternative(missing_tool: str, host_facts: HostFacts) -> str | None:
    """Find a working alternative for a missing tool."""
    alternatives = ToolAlternatives.get_alternatives(missing_tool)
    
    for alt in alternatives:
        if alt in host_facts.available_binaries:
            return alt
    
    return None


def get_package_manager(host_facts: HostFacts) -> str | None:
    """Detect the system's package manager, preferring the pre-detected value."""
    # Use the cached value gathered at startup if available
    if host_facts.package_manager:
        return host_facts.package_manager

    package_managers = {
        "apt": ["apt", "apt-get"],
        "dnf": ["dnf"],
        "yum": ["yum"],
        "pacman": ["pacman"],
        "zypper": ["zypper"],
    }
    for pm_name, binaries in package_managers.items():
        if any(binary in host_facts.available_binaries for binary in binaries):
            return pm_name

    return None


def create_tool_installation_actions(
    missing_tools: set[str], 
    host_facts: HostFacts
) -> list[ShellAction]:
    """Create actions to install missing tools."""
    actions = []
    package_manager = get_package_manager(host_facts)
    
    if not package_manager:
        logger.warning("Could not detect package manager for auto-installation")
        return actions
    
    installable_tools = []
    for tool in missing_tools:
        if ToolAlternatives.can_install(tool):
            package = ToolAlternatives.get_install_package(tool, package_manager)
            if package:
                installable_tools.append((tool, package))
    
    if not installable_tools:
        return actions
    
    # Create installation command based on package manager
    if package_manager in ["apt", "apt-get"]:
        # Update package list first
        actions.append(ShellAction(
            type=ActionType.shell,
            description="Update package list",
            command=f"{package_manager} update",
            requires_sudo=True,
            risky=False
        ))
        
        # Install packages
        packages = [pkg for _, pkg in installable_tools]
        tools_str = ", ".join([tool for tool, _ in installable_tools])
        actions.append(ShellAction(
            type=ActionType.shell,
            description=f"Install missing tools: {tools_str}",
            command=f"{package_manager} install -y {' '.join(packages)}",
            requires_sudo=True,
            risky=False
        ))
    
    elif package_manager in ["dnf", "yum"]:
        packages = [pkg for _, pkg in installable_tools]
        tools_str = ", ".join([tool for tool, _ in installable_tools])
        actions.append(ShellAction(
            type=ActionType.shell,
            description=f"Install missing tools: {tools_str}",
            command=f"{package_manager} install -y {' '.join(packages)}",
            requires_sudo=True,
            risky=False
        ))
    
    elif package_manager == "pacman":
        packages = [pkg for _, pkg in installable_tools]
        tools_str = ", ".join([tool for tool, _ in installable_tools])
        actions.append(ShellAction(
            type=ActionType.shell,
            description=f"Install missing tools: {tools_str}",
            command=f"pacman -S --noconfirm {' '.join(packages)}",
            requires_sudo=True,
            risky=False
        ))
    
    elif package_manager == "zypper":
        packages = [pkg for _, pkg in installable_tools]
        tools_str = ", ".join([tool for tool, _ in installable_tools])
        actions.append(ShellAction(
            type=ActionType.shell,
            description=f"Install missing tools: {tools_str}",
            command=f"zypper install -y {' '.join(packages)}",
            requires_sudo=True,
            risky=False
        ))
    
    return actions


def substitute_alternatives_in_plan(
    plan: Plan, 
    host_facts: HostFacts,
    tool_substitutions: dict[str, str]
) -> Plan:
    """Substitute alternative tools in the plan."""
    if not tool_substitutions:
        return plan
    
    new_actions = []
    
    for action in plan.actions:
        if action.type == "shell":
            argv = list(action.argv)

            if argv:
                binary = argv[0]

                if binary in tool_substitutions:
                    new_binary = tool_substitutions[binary]
                    argv[0] = new_binary
                    new_command = " ".join(argv)

                    new_action = ShellAction(
                        type=action.type,
                        description=action.description + f" (using {new_binary} instead of {binary})",
                        command=new_command,
                        requires_sudo=action.requires_sudo,
                        risky=action.risky,
                    )
                    new_actions.append(new_action)
                else:
                    new_actions.append(action)
            else:
                new_actions.append(action)
        else:
            new_actions.append(action)
    
    # Create new plan with substituted actions
    return Plan(
        user_request=plan.user_request,
        distro_hint=plan.distro_hint,
        init_system=plan.init_system,
        actions=new_actions,
        notes=plan.notes
    )


def regenerate_plan_with_alternatives(
    original_request: str,
    host_facts: HostFacts,
    missing_tools: set[str],
    available_alternatives: dict[str, str]
) -> Plan | None:
    """Regenerate the plan, telling the LLM which tools are missing.

    Works even when no known alternatives exist — the LLM is told the tool
    is absent and must find its own approach using available_binaries.
    """
    if not missing_tools:
        return None

    parts = [
        f"IMPORTANT: The following tools are NOT available on this system and must NOT be used: {', '.join(sorted(missing_tools))}."
    ]
    if available_alternatives:
        subs = "; ".join(f"use '{alt}' instead of '{m}'" for m, alt in available_alternatives.items())
        parts.append(f"Known substitutions: {subs}.")
    parts.append(
        "Use only the binaries listed in the host system information. "
        "Choose an alternative approach that works with what is available."
    )

    context_hint = " ".join(parts)
    enhanced_request = f"{original_request}\n\nSystem constraints: {context_hint}"
    
    try:
        messages, tools, _pf = create_plan_generation_request(enhanced_request, host_facts)
        new_plan = call_llm_structured(
            messages=messages,
            tools=tools,
            function_name="emit_plan",
            result_model=Plan,
        )
        return new_plan
    except Exception as e:
        logger.error(f"Failed to regenerate plan with alternatives: {e}")
        return None


def adaptive_plan_generation(
    user_request: str,
    host_facts: HostFacts,
    auto_install: bool = True
) -> tuple[Plan, list[str]]:
    """
    Generate a plan that adapts to missing tools.
    
    Args:
        user_request: The user's natural language request
        host_facts: Information about the host system
        auto_install: Whether to auto-install missing tools
    
    Returns:
        Tuple of (adapted_plan, list_of_adaptation_messages)
    """
    adaptation_messages = []
    
    try:
        messages, tools, has_prefetch = create_plan_generation_request(user_request, host_facts)
        original_plan = call_llm_with_lookups(
            messages=messages,
            tools=tools,
            host_facts=host_facts,
            result_model=Plan,
            has_prefetch=has_prefetch,
        )
    except Exception as e:
        raise PlannerError(f"Failed to generate initial plan: {e}")
    
    # Detect missing tools
    missing_tools = detect_missing_tools(original_plan, host_facts)
    
    if not missing_tools:
        # No missing tools, return original plan
        return original_plan, adaptation_messages
    
    logger.info(f"Detected missing tools: {missing_tools}")
    adaptation_messages.append(f"Missing tools detected: {', '.join(missing_tools)}")
    
    # Strategy 1: Find working alternatives
    tool_substitutions = {}
    remaining_missing = set()
    
    for tool in missing_tools:
        alternative = find_working_alternative(tool, host_facts)
        if alternative:
            tool_substitutions[tool] = alternative
            adaptation_messages.append(f"Using '{alternative}' instead of '{tool}'")
        else:
            remaining_missing.add(tool)
    
    # Strategy 2: Auto-install remaining missing tools (if enabled)
    install_actions = []
    if auto_install and remaining_missing:
        install_actions = create_tool_installation_actions(remaining_missing, host_facts)
        if install_actions:
            installable = [tool for tool in remaining_missing if ToolAlternatives.can_install(tool)]
            if installable:
                adaptation_messages.append(f"Will install missing tools: {', '.join(installable)}")
            
            # Remove installable tools from remaining missing
            remaining_missing = remaining_missing - set(installable)
    
    # Strategy 3: Regenerate — always ask the LLM to find another approach
    # when any tool is missing, whether or not we know a named substitute.
    available_alternatives = dict(tool_substitutions)  # may be empty, that's OK
    regenerated_plan = regenerate_plan_with_alternatives(
        user_request, host_facts, missing_tools, available_alternatives
    )

    if regenerated_plan:
        final_plan = regenerated_plan
        adaptation_messages.append("Regenerated plan using available tools")
    else:
        # Fallback: use the original plan with simple substitutions applied
        final_plan = substitute_alternatives_in_plan(original_plan, host_facts, tool_substitutions) if tool_substitutions else original_plan
        if remaining_missing:
            adaptation_messages.append(f"Warning: could not adapt plan for missing tools: {', '.join(remaining_missing)}")
    
    # Prepend installation actions if any
    if install_actions:
        final_plan.actions = install_actions + final_plan.actions
    
    return final_plan, adaptation_messages
