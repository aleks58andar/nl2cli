"""Preview rendering for execution plans."""


from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from src.core.schema import Plan, EditFileAction, ShellAction, RestartServiceAction, HostFacts


def render_preview(plan: Plan, host_facts: HostFacts) -> str:
    """
    Render a human-readable preview of the execution plan.
    
    Args:
        plan: The plan to render
        host_facts: Information about the host system for context
    
    Returns:
        Formatted preview text
    """
    if not plan.actions:
        return "# No actions to execute"
    
    lines = []
    lines.append(f"# Plan: {plan.user_request}")
    
    if plan.distro_hint:
        lines.append(f"# Target system: {plan.distro_hint} ({plan.init_system})")
    
    if plan.notes:
        lines.append(f"# Notes: {plan.notes}")
    
    lines.append("")
    
    for i, action in enumerate(plan.actions, 1):
        action_lines = _render_action(action, i, host_facts)
        lines.extend(action_lines)
        lines.append("")  # Add spacing between actions
    
    return "\n".join(lines)


def _render_action(action, action_num: int, host_facts: HostFacts) -> list[str]:
    """Render a single action as commented preview lines."""
    lines = []
    
    # Add action header
    sudo_indicator = " (requires sudo)" if action.requires_sudo else ""
    risk_indicator = " ⚠️ RISKY" if action.risky else ""
    lines.append(f"# Action {action_num}: {action.description}{sudo_indicator}{risk_indicator}")
    
    if isinstance(action, EditFileAction):
        lines.extend(_render_edit_file_action(action))
    elif isinstance(action, ShellAction):
        lines.extend(_render_shell_action(action))
    elif isinstance(action, RestartServiceAction):
        lines.extend(_render_service_action(action, host_facts))
    
    return lines


def _render_edit_file_action(action: EditFileAction) -> list[str]:
    """Render file edit action preview."""
    lines = []
    
    # Show backup operation if enabled
    if action.backup:
        backup_cmd = f"sudo cp '{action.path}' '{action.path}.bak'" if action.requires_sudo else f"cp '{action.path}' '{action.path}.bak'"
        lines.append(backup_cmd)
    
    # Show the edit operation based on mode
    if action.mode == "replace_line":
        if action.selector and action.after:
            # Show as sed command for readability
            sed_cmd = f"sed -i 's|{action.selector}|{action.after}|' '{action.path}'"
            if action.requires_sudo:
                sed_cmd = f"sudo {sed_cmd}"
            lines.append(sed_cmd)
        else:
            lines.append(f"# Edit {action.path} (replace line matching pattern)")
    
    elif action.mode == "insert":
        if action.after:
            if action.selector:
                lines.append(f"# Insert '{action.after}' after line matching '{action.selector}' in {action.path}")
            else:
                lines.append(f"# Append '{action.after}' to {action.path}")
        else:
            lines.append(f"# Insert content into {action.path}")
    
    elif action.mode == "ensure_kv":
        if action.ensure_key and action.ensure_value:
            lines.append(f"# Ensure '{action.ensure_key} = {action.ensure_value}' in {action.path}")
        else:
            lines.append(f"# Ensure key-value setting in {action.path}")
    
    return lines


def _render_shell_action(action: ShellAction) -> list[str]:
    """Render shell action preview."""
    command = action.command
    
    # Add sudo if required and not already present
    if action.requires_sudo and not command.strip().startswith("sudo "):
        command = f"sudo {command}"
    
    return [command]


def _render_service_action(action: RestartServiceAction, host_facts: HostFacts) -> list[str]:
    """Render service action preview based on init system."""
    service_name = action.service_name
    
    if host_facts.init_system == "systemd":
        return [f"sudo systemctl restart {service_name}"]
    elif host_facts.init_system == "sysv":
        return [f"sudo /etc/init.d/{service_name} restart"]
    elif host_facts.init_system == "upstart":
        return [f"sudo initctl restart {service_name}"]
    else:
        # Generic fallback
        return [f"sudo service {service_name} restart"]


def render_rich_preview(plan: Plan, host_facts: HostFacts) -> None:
    """
    Render a rich, colored preview using the Rich library.
    
    Args:
        plan: The plan to render
        host_facts: Information about the host system
    """
    console = Console()
    
    if not plan.actions:
        console.print("[yellow]No actions to execute[/yellow]")
        return
    
    # Title
    title = Text(f"Execution Plan: {plan.user_request}", style="bold blue")
    console.print(title)
    
    # System info
    if plan.distro_hint:
        console.print(f"[dim]Target: {plan.distro_hint} ({plan.init_system})[/dim]")
    
    if plan.notes:
        console.print(Panel(plan.notes, title="Notes", style="yellow"))
    
    console.print()
    
    # Actions
    for i, action in enumerate(plan.actions, 1):
        _render_rich_action(console, action, i, host_facts)


def _render_rich_action(console: Console, action, action_num: int, host_facts: HostFacts) -> None:
    """Render a single action with rich formatting."""
    # Create action header
    header_parts = [f"Action {action_num}"]
    
    if action.requires_sudo:
        header_parts.append("[red]sudo[/red]")
    
    if action.risky:
        header_parts.append("[yellow]⚠️  risky[/yellow]")
    
    header = " ".join(header_parts)
    
    # Get the preview commands
    if isinstance(action, EditFileAction):
        commands = _render_edit_file_action(action)
    elif isinstance(action, ShellAction):
        commands = _render_shell_action(action)
    elif isinstance(action, RestartServiceAction):
        commands = _render_service_action(action, host_facts)
    else:
        commands = ["# Unknown action type"]
    
    # Create the panel content
    description = Text(action.description, style="bold")
    
    if commands:
        # Syntax highlight the commands
        command_text = "\n".join(commands)
        syntax = Syntax(command_text, "bash", theme="monokai", line_numbers=False)
        
        # Create a renderable group instead of string concatenation
        from rich.console import Group
        panel_content = Group(description, "", syntax)
    else:
        panel_content = description
    
    # Show the panel
    console.print(Panel(
        panel_content,
        title=header,
        border_style="blue" if not action.risky else "yellow"
    ))
    console.print()


def create_script_preview(plan: Plan, host_facts: HostFacts) -> str:
    """
    Create a shell script representation of the plan.
    
    Args:
        plan: The plan to convert
        host_facts: Information about the host system
    
    Returns:
        Shell script content
    """
    lines = [
        "#!/bin/bash",
        "# Generated by nl2cli",
        f"# Plan: {plan.user_request}",
        ""
    ]
    
    if plan.notes:
        lines.extend([
            "# Notes:",
            f"# {plan.notes}",
            ""
        ])
    
    lines.extend([
        "set -e  # Exit on error",
        "set -u  # Exit on undefined variable",
        ""
    ])
    
    for i, action in enumerate(plan.actions, 1):
        lines.append(f"echo 'Executing action {i}: {action.description}'")
        
        if isinstance(action, EditFileAction):
            if action.backup:
                backup_cmd = f"cp '{action.path}' '{action.path}.bak'"
                if action.requires_sudo:
                    backup_cmd = f"sudo {backup_cmd}"
                lines.append(backup_cmd)
            
            # For script mode, use Python for reliable edits
            lines.append("# File edit would be performed by Python code")
            
        elif isinstance(action, ShellAction):
            command = action.command
            if action.requires_sudo and not command.startswith("sudo "):
                command = f"sudo {command}"
            lines.append(command)
            
        elif isinstance(action, RestartServiceAction):
            if host_facts.init_system == "systemd":
                lines.append(f"sudo systemctl restart {action.service_name}")
            elif host_facts.init_system == "sysv":
                lines.append(f"sudo /etc/init.d/{action.service_name} restart")
            else:
                lines.append(f"sudo service {action.service_name} restart")
        
        lines.append("")
    
    lines.append("echo 'Plan execution completed!'")
    
    return "\n".join(lines)
