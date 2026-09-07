"""OpenAI API client for structured plan generation.

Supports a multi-turn tool-calling loop: the LLM can call lightweight
lookup tools (get_tool_usage, check_path, get_service_info) to gather
context on demand, then emit_plan when it's ready.  This keeps the
upfront prompt small and avoids sending --help dumps for every request.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from src.config import get_config
from src.schema import Plan, HostFacts

T = TypeVar('T', bound=BaseModel)

logger = logging.getLogger(__name__)

MAX_FALLBACK_ROUNDS = 3


# ---------------------------------------------------------------------------
# OpenAI wrapper
# ---------------------------------------------------------------------------

class ModelClientError(Exception):
    """Raised when the LLM call fails or returns an unusable response."""


class OpenAIClient:
    def __init__(self, api_key: str | None = None):
        config = get_config()
        if not api_key:
            api_key = config.api_key
        if not api_key:
            raise ModelClientError(
                "OpenAI API key not found. Set OPENAI_API_KEY or configure in config file."
            )
        self.client = OpenAI(api_key=api_key)
        self.config = config.model

    # Single-shot forced call (kept for backward compat / adaptive_planner)
    def call_structured(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        function_name: str,
        result_model: Type[T],
    ) -> T:
        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=tools,
                tool_choice={"type": "function", "function": {"name": function_name}},
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                timeout=self.config.timeout,
            )
            if not response.choices:
                raise ModelClientError("No response choices returned from API")
            message = response.choices[0].message
            if not message.tool_calls:
                raise ModelClientError("No tool calls in response")
            tool_call = message.tool_calls[0]
            if tool_call.function.name != function_name:
                raise ModelClientError(
                    f"Expected '{function_name}', got '{tool_call.function.name}'"
                )
            function_args = json.loads(tool_call.function.arguments)
            return result_model(**function_args)
        except ModelClientError:
            raise
        except Exception as e:
            raise ModelClientError(f"OpenAI API call failed: {e}") from e

    def call_with_lookups(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        host_facts: HostFacts,
        result_model: Type[T],
        plan_function_name: str = "emit_plan",
        has_prefetch: bool = False,
    ) -> T:
        """Generate a plan in 1-2 API calls.

        If pre-fetched context is present, forces emit_plan on the first call
        (single API call). Otherwise allows up to MAX_FALLBACK_ROUNDS of
        lookups before forcing the plan.
        """
        if has_prefetch:
            # Pre-fetched data is in the messages — force plan immediately
            return self._forced_plan_call(
                messages, tools, plan_function_name, result_model
            )

        # No pre-fetch: allow a few lookup rounds, then force
        for _round in range(MAX_FALLBACK_ROUNDS):
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                timeout=self.config.timeout,
            )
            if not response.choices:
                raise ModelClientError("No response choices")
            msg = response.choices[0].message
            if not msg.tool_calls:
                # LLM responded conversationally (e.g. for informational queries).
                # Force it to emit a plan — it should run a command to get the
                # information and let the summariser speak the result.
                logger.debug("LLM returned text with no tool call — forcing emit_plan")
                if msg.content:
                    messages.append({"role": "assistant", "content": msg.content})
                messages.append({
                    "role": "user",
                    "content": (
                        "You must always call emit_plan, even for informational requests. "
                        "If you need to look something up, emit a plan with a read-only "
                        "shell command (e.g. uname -r, df -h, free -h). "
                        "The command output will be spoken back to the user. "
                        "Call emit_plan now."
                    ),
                })
                return self._forced_plan_call(messages, tools, plan_function_name, result_model)

            # Check if emit_plan was called
            for tc in msg.tool_calls:
                if tc.function.name == plan_function_name:
                    args = json.loads(tc.function.arguments)
                    return result_model(**args)

            # Resolve lookups
            messages.append({"role": "assistant", "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]})
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                result_text = _execute_lookup(tc.function.name, args, host_facts)
                compact = ", ".join(f"{k}={v}" for k, v in args.items())
                logger.info("LLM lookup: %s(%s)", tc.function.name, compact)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                })

        # Out of rounds — force emit_plan
        return self._forced_plan_call(
            messages, tools, plan_function_name, result_model
        )

    def _forced_plan_call(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        plan_function_name: str,
        result_model: Type[T],
    ) -> T:
        """Single API call with tool_choice forced to emit_plan."""
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            tools=tools,
            tool_choice={"type": "function",
                         "function": {"name": plan_function_name}},
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            timeout=self.config.timeout,
        )
        if not response.choices:
            raise ModelClientError("No response choices")
        msg = response.choices[0].message
        if not msg.tool_calls:
            raise ModelClientError("No tool calls in forced response")
        tc = msg.tool_calls[0]
        try:
            args = json.loads(tc.function.arguments)
        except json.JSONDecodeError as e:
            raise ModelClientError(f"Bad JSON from {plan_function_name}: {e}")
        try:
            return result_model(**args)
        except Exception as e:
            raise ModelClientError(f"Plan validation failed: {e}")


# ---------------------------------------------------------------------------
# Lookup tool definitions (exposed to the LLM)
# ---------------------------------------------------------------------------

LOOKUP_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_tool_usage",
            "description": (
                "Get CLI usage/help for a binary installed on this system. "
                "Call this BEFORE using an uncommon tool so you know the "
                "exact subcommands and flags. You can optionally pass a "
                "subcommand to get its specific help."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Binary name, e.g. 'system76-power'",
                    },
                    "subcommand": {
                        "type": "string",
                        "description": "Optional subcommand, e.g. 'profile'",
                    },
                },
                "required": ["tool_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_path",
            "description": (
                "Check whether a file or directory exists on the host. "
                "Returns the path status and, for small files (<80 lines), "
                "also returns the file contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to check, e.g. '/etc/systemd/system/system76-power.service'",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_sysfs",
            "description": (
                "Discover hardware control interfaces in sysfs. "
                "Use this when a CLI tool doesn't expose the control you need "
                "(e.g. keyboard backlight, screen brightness, fan speed). "
                "Pass a glob pattern like 'leds/*kbd*' or 'backlight/*'. "
                "Returns matching paths and their current values."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern relative to /sys/class/, "
                            "e.g. 'leds/*kbd*' or 'backlight/*'"
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_info",
            "description": (
                "Get information about a systemd service: its unit file "
                "path, current state (active/inactive/enabled/disabled), "
                "and the unit file contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "Service name without .service suffix, e.g. 'system76-power'",
                    },
                },
                "required": ["service_name"],
            },
        },
    },
]

LOOKUP_TOOL_NAMES: set[str] = {t["function"]["name"] for t in LOOKUP_TOOLS}

_SYS_CLASS = Path("/sys/class")


# ---------------------------------------------------------------------------
# Lookup executors (run locally, never shell=True)
# ---------------------------------------------------------------------------

def _execute_lookup(
    tool_name: str, args: dict[str, Any], host_facts: HostFacts
) -> str:
    if tool_name == "get_tool_usage":
        return _lookup_tool_usage(
            args.get("tool_name", ""),
            args.get("subcommand"),
            host_facts,
        )
    elif tool_name == "check_path":
        return _lookup_check_path(args.get("path", ""))
    elif tool_name == "check_sysfs":
        return _lookup_check_sysfs(args.get("pattern", ""))
    elif tool_name == "get_service_info":
        return _lookup_service_info(args.get("service_name", ""))
    else:
        return f"Unknown lookup tool: {tool_name}"


def _lookup_tool_usage(
    tool_name: str, subcommand: str | None, host_facts: HostFacts
) -> str:
    if not tool_name:
        return "Error: tool_name is required"
    if tool_name not in host_facts.available_binaries:
        return f"'{tool_name}' is not installed on this system."

    argv = [tool_name]
    if subcommand:
        argv.append(subcommand)
    argv.append("--help")

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=5
        )
        text = (proc.stdout or proc.stderr or "").strip()
        if not text:
            return f"No help output from: {' '.join(argv)}"
        lines = text.splitlines()[:40]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error running {' '.join(argv)}: {exc}"


def _lookup_check_path(path_str: str) -> str:
    if not path_str:
        return "Error: path is required"
    p = Path(path_str)
    if not p.exists():
        return f"Does not exist: {path_str}"

    parts = []
    if p.is_dir():
        parts.append(f"Directory exists: {path_str}")
        try:
            children = sorted(p.iterdir())[:50]
            parts.append("Contents:")
            for c in children:
                kind = "d" if c.is_dir() else "f"
                parts.append(f"  [{kind}] {c.name}")
        except PermissionError:
            parts.append("  (permission denied to list — directory exists but is root-owned)")
    else:
        parts.append(f"File exists: {path_str}")
        try:
            text = p.read_text(errors="replace")
            lines = text.splitlines()
            if len(lines) <= 80:
                parts.append("Contents:")
                parts.append(text)
            else:
                parts.append(f"File has {len(lines)} lines (showing first 40):")
                parts.append("\n".join(lines[:40]))
        except PermissionError:
            parts.append(
                "(permission denied — file is root-owned. "
                "Use edit_file with requires_sudo=true to modify it. "
                "Do NOT call check_path again for this file.)"
            )

    return "\n".join(parts)


def _lookup_check_sysfs(pattern: str) -> str:
    """Glob /sys/class/<pattern> and return matching paths + their readable values."""
    import glob as _glob

    if not pattern:
        return "Error: pattern is required"

    # Strip leading /sys/class/ if the user included it
    pattern = pattern.lstrip("/")
    if pattern.startswith("sys/class/"):
        pattern = pattern[len("sys/class/"):]

    full_pattern = f"/sys/class/{pattern}"
    matches = sorted(_glob.glob(full_pattern))

    if not matches:
        return f"No sysfs entries match: {full_pattern}"

    lines = [f"Matches for {full_pattern}:"]
    for m in matches[:20]:
        p = Path(m)
        if p.is_symlink():
            p = p.resolve()
        if p.is_dir():
            lines.append(f"\n[dir] {m}")
            # Show interesting readable files within the entry
            for attr in ("brightness", "max_brightness", "actual_brightness",
                         "bl_power", "power/control", "trigger"):
                attr_path = p / attr
                if attr_path.exists():
                    try:
                        val = attr_path.read_text().strip()
                        lines.append(f"  {attr} = {val}")
                    except PermissionError:
                        lines.append(f"  {attr} = (permission denied)")
        else:
            try:
                val = p.read_text().strip()
                lines.append(f"[file] {m} = {val}")
            except PermissionError:
                lines.append(f"[file] {m} = (permission denied)")

    lines.append(
        "\nTo set a value, use an edit_file action on the full sysfs path "
        "(requires_sudo=true, mode=insert, after=<value>)."
    )
    return "\n".join(lines)


def _lookup_service_info(service_name: str) -> str:
    if not service_name:
        return "Error: service_name is required"

    name = service_name.removesuffix(".service")
    parts = []

    # Find unit file path
    search_dirs = [
        Path("/etc/systemd/system"),
        Path("/usr/lib/systemd/system"),
        Path("/lib/systemd/system"),
    ]
    unit_path: Path | None = None
    for d in search_dirs:
        candidate = d / f"{name}.service"
        if candidate.exists():
            unit_path = candidate
            break

    if unit_path:
        parts.append(f"Unit file: {unit_path}")
        try:
            parts.append(unit_path.read_text(errors="replace"))
        except PermissionError:
            parts.append("(permission denied)")

        # Check for drop-in directory
        dropin_dir = unit_path.parent / f"{name}.service.d"
        if dropin_dir.is_dir():
            parts.append(f"\nDrop-in directory exists: {dropin_dir}")
            for f in sorted(dropin_dir.iterdir()):
                parts.append(f"  {f.name}")
                try:
                    parts.append(f.read_text(errors="replace"))
                except PermissionError:
                    parts.append("  (permission denied)")
        else:
            parts.append(f"\nDrop-in directory ({dropin_dir}) does not exist yet.")
    else:
        parts.append(f"No unit file found for '{name}.service'")

    # Get status via systemctl
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", f"{name}.service"],
            capture_output=True, text=True, timeout=5,
        )
        parts.append(f"\nStatus: {proc.stdout.strip()}")
    except Exception:
        pass

    try:
        proc = subprocess.run(
            ["systemctl", "is-enabled", f"{name}.service"],
            capture_output=True, text=True, timeout=5,
        )
        parts.append(f"Enabled: {proc.stdout.strip()}")
    except Exception:
        pass

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# System prompt + few-shot examples
# ---------------------------------------------------------------------------

def get_system_prompt() -> str:
    return """You are a Linux system administration expert that converts natural language requests into structured, safe execution plans.

You have lookup tools to inspect the host before planning. Relevant lookup results are often pre-fetched and shown in the conversation — use that data directly and skip redundant lookups.

Available lookup tools (use sparingly, only when pre-fetched data is insufficient):
- get_tool_usage(tool_name, subcommand?) — CLI help for a binary
- check_path(path) — whether a file/dir exists; shows contents for small files
- check_sysfs(pattern) — discover hardware controls in /sys/class/<pattern>
- get_service_info(service_name) — systemd unit file and drop-in contents

IMPORTANT: Never call the same lookup twice. If a tool doesn't support the needed feature after one lookup, switch approach (sysfs, udev rule, different binary). Call emit_plan as soon as you have enough information.

CRITICAL REQUIREMENTS:
1. ONLY return plans that are safe and reversible
2. ALWAYS prefer systemd over legacy init systems when available
3. NEVER suggest commands that could damage the system
4. Use explicit file paths and service names
5. Include backup operations for any file modifications
6. Mark risky operations as risky=true
7. If unsure about safety, add detailed notes and refuse the operation
8. Do NOT include sudo in any command — set requires_sudo=true instead

SHELL COMMAND RULES:
- FORBIDDEN in all commands: ; && || > >> < ` $( ) &  (chaining, redirection, subshells)
- FORBIDDEN: bash -c, sh -c (except the system uses it internally for pipes — do not emit it)
- FORBIDDEN: sudo in command strings — use requires_sudo=true instead
- Pipes (|) are ALLOWED only in read-only, non-sudo commands where the pipe chain reads data
  and does not write to any file or run any privileged operation.
  Example: "find ~/Downloads -type f -printf '%s\t%p\n' | sort -rn | head -5"
  Example: "df -h | grep /dev"
- For anything that chains write or privileged operations: split into separate actions.
- WRONG: "systemctl is-enabled bluetooth || true"  (|| forbidden)
- RIGHT: "systemctl is-enabled bluetooth"
- WRONG: "rfkill list | sudo tee /file"  (pipe + sudo write forbidden)
- RIGHT: "rfkill list | grep bluetooth"  (read-only pipe, fine)

INFORMATIONAL QUERIES: When the user asks a question ("what kernel am I on?", "how much disk space?"),
you MUST still call emit_plan with a read-only shell command that answers the question.
The command output will be summarized and spoken back. Never respond with plain text.
Examples:
- "what kernel version" → emit_plan with: uname -r
- "how much free disk space" → emit_plan with: df -h
- "how much RAM" → emit_plan with: free -h
- "what's my CPU" → emit_plan with: lscpu | head -10
- "largest file in Downloads" → emit_plan with: find ~/Downloads -type f -printf '%s\t%p\n' | sort -rn | head -5

DISK USAGE WARNING: NEVER run `du` without a specific target directory (e.g. `du -sh ~/Downloads`).
Bare `du` or `du /` recurses into /proc, /sys, and /dev, generating GB of output and permission errors.
For FREE DISK SPACE always use `df -h`. Reserve `du` for "how big is this folder" questions with a specific path.

PERSISTENCE — CRITICAL:
When the user says "by default", "always", "every time", "on boot", "when X happens", or "permanently":
- Do NOT just write to sysfs — those values are reset by power management tools on every profile/power change.
- Use a PERSISTENT mechanism: a systemd drop-in ExecStartPost= on the EXISTING service that manages the relevant hardware (e.g. com.system76.PowerDaemon for power/LEDs, bluetooth for bluetooth).
- The ExecStartPost shell command must be valid shell (e.g. ExecStartPost=/bin/sh -c "echo 1 > /sys/class/leds/.../brightness").
- Do NOT create standalone oneshot services for this — they only run at boot, not on runtime profile changes.
- Direct sysfs writes (edit_file on /sys/…) are ONLY appropriate for "set it right now" one-off requests, never for "by default" requests.
- Note: runtime sysfs side effects are automatically restored by the execution environment — your persistent fix only needs to cover boot/service-restart scenarios.

SYSTEMD BEST PRACTICES:
- For service customisation, ALWAYS use a drop-in override at <unit>.service.d/override.conf.
- NEVER copy or replace a unit file from /usr/lib to /etc.
- Call get_service_info first to see the current unit file and whether a drop-in dir exists.
- After creating or modifying any unit file or drop-in, ALWAYS add a "systemctl daemon-reload" action so systemd picks up the changes.

CAPABILITIES:
- File editing with modes:
  * insert — append or inject content into a file. Use this to ADD new lines (e.g. a new ExecStartPost= to a systemd drop-in). Set 'after' to the full content to insert.
  * replace_line — replace an existing line. REQUIRES 'selector' (a regex that matches the line to replace). Only use this when you know the exact line that exists.
  * ensure_kv — set a key=value pair, creating or updating it.
- Note: shell syntax inside file content (e.g. echo 2 > /path) is valid — the shell restrictions only apply to ShellAction commands, not to text written into files.
- Shell command execution (single commands only, no operators)
- Service management via restart_service action

SAFETY GUIDELINES:
- No rm -rf commands
- No disk formatting or partitioning
- No bootloader modifications
- No direct kernel module operations
- No network configuration that could lock out access
- Always create backups before editing files

OUTPUT FORMAT:
Call emit_plan with a complete Plan object when ready.
"""


def get_few_shot_examples() -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": "enable bluetooth"
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "type": "function",
                "function": {
                    "name": "emit_plan",
                    "arguments": json.dumps({
                        "user_request": "enable bluetooth",
                        "init_system": "systemd",
                        "actions": [
                            {
                                "type": "shell",
                                "description": "Enable bluetooth service at boot",
                                "command": "systemctl enable bluetooth",
                                "requires_sudo": True
                            },
                            {
                                "type": "restart_service",
                                "description": "Start bluetooth service",
                                "service_name": "bluetooth",
                                "requires_sudo": True
                            }
                        ]
                    })
                }
            })
        },
        {
            "role": "user",
            "content": "stop my bluetooth"
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "type": "function",
                "function": {
                    "name": "emit_plan",
                    "arguments": json.dumps({
                        "user_request": "stop my bluetooth",
                        "init_system": "systemd",
                        "actions": [
                            {
                                "type": "shell",
                                "description": "Disable bluetooth service at boot",
                                "command": "systemctl disable bluetooth",
                                "requires_sudo": True
                            },
                            {
                                "type": "restart_service",
                                "description": "Stop bluetooth service",
                                "service_name": "bluetooth",
                                "requires_sudo": True
                            }
                        ]
                    })
                }
            })
        },
        {
            "role": "user",
            "content": "change ssh port to 2222"
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "type": "function",
                "function": {
                    "name": "emit_plan",
                    "arguments": json.dumps({
                        "user_request": "change ssh port to 2222",
                        "init_system": "systemd",
                        "actions": [
                            {
                                "type": "edit_file",
                                "description": "Change SSH port to 2222",
                                "path": "/etc/ssh/sshd_config",
                                "backup": True,
                                "requires_sudo": True,
                                "mode": "replace_line",
                                "selector": "^\\s*#?\\s*Port\\s+.*",
                                "after": "Port 2222"
                            },
                            {
                                "type": "restart_service",
                                "description": "Restart SSH service",
                                "service_name": "ssh",
                                "requires_sudo": True
                            }
                        ],
                        "notes": "Ensure firewall allows port 2222 before disconnecting."
                    })
                }
            }),
        },
    ]


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_host_context(host_facts: HostFacts) -> str:
    """Build a compact host-info block for the system message.

    Only includes cheap, broadly-useful facts. Heavy data (--help output,
    service file contents) is available via lookup tools on demand.
    """
    lines: list[str] = []

    if host_facts.machine_vendor or host_facts.machine_model:
        hw = " ".join(filter(None, [host_facts.machine_vendor, host_facts.machine_model]))
        lines.append(f"Hardware: {hw}")
    lines.append(f"Laptop: {'yes' if host_facts.is_laptop else 'no'}")
    lines.append(
        f"OS: {host_facts.distro_id} {host_facts.distro_version} "
        f"(kernel {host_facts.kernel_version})"
    )
    lines.append(f"Init: {host_facts.init_system}")
    if host_facts.package_manager:
        lines.append(f"Package manager: {host_facts.package_manager}")
    if host_facts.active_services:
        lines.append(f"Active services: {', '.join(sorted(host_facts.active_services))}")
    if host_facts.available_binaries:
        lines.append(
            f"Available binaries: {', '.join(sorted(host_facts.available_binaries))}"
        )

    return "\n".join(lines)


def _prefetch_context(nl_request: str, host_facts: HostFacts) -> list[dict]:
    """Eagerly run lookups based on request keywords and inject results.

    Returns a list of (assistant tool_call, tool result) message pairs that
    are injected into the conversation before the first LLM call, so the
    model starts with full context rather than needing exploratory rounds.

    This is general: it looks at the request text, not the specific domain.
    """
    req = nl_request.lower()
    prefetched: list[dict] = []
    seen: set[str] = set()

    def _inject(tool_name: str, args: dict[str, Any]) -> None:
        cache_key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
        if cache_key in seen:
            return
        seen.add(cache_key)
        result = _execute_lookup(tool_name, args, host_facts)
        tc_id = f"prefetch_{len(seen)}"
        compact = ", ".join(f"{k}={v}" for k, v in args.items())
        logger.debug("Pre-fetching %s(%s)", tool_name, compact)
        prefetched.append({
            "role": "assistant",
            "tool_calls": [{"id": tc_id, "type": "function",
                            "function": {"name": tool_name,
                                         "arguments": json.dumps(args)}}],
        })
        prefetched.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": result,
        })

    # 1. Sysfs hardware — look up anything matching broad categories in the request
    # More specific patterns take priority; broad "leds/*" only fires when no
    # specific LED keyword matched (avoids redundant broad + narrow lookups)
    sysfs_matched: set[str] = set()

    def _inject_sysfs(keywords: tuple, pattern: str) -> None:
        if any(k in req for k in keywords):
            # Don't add a broad pattern if a narrower one for the same class already matched
            cls = pattern.split("/")[0]
            if cls in sysfs_matched and pattern.endswith("*"):
                return
            sysfs_matched.add(cls)
            _inject("check_sysfs", {"pattern": pattern})

    _inject_sysfs(("keyboard", "kbd", "key backlight", "key light"), "leds/*kbd*")
    _inject_sysfs(("backlight", "brightness", "screen", "display", "dim"), "backlight/*")
    _inject_sysfs(("battery", "power supply", "charging"), "power_supply/*")
    # Generic LED only if no specific LED keyword already matched
    if "leds" not in sysfs_matched:
        _inject_sysfs(("led", " light", "indicator", "glow"), "leds/*")

    # 2. Tool help — look up uncommon binaries mentioned by name in the request
    # Skip short names (2 chars like "ss") and common coreutils to avoid false positives
    _skip_binaries = {"systemctl", "service", "apt", "apt-get", "tee", "cp",
                      "cat", "echo", "mkdir", "rm", "mv", "ls", "grep", "ss",
                      "ip", "cd", "pwd", "find", "sed", "awk"}
    for binary in host_facts.available_binaries:
        if len(binary) > 4 and binary in req and binary not in _skip_binaries:
            _inject("get_tool_usage", {"tool_name": binary})

    # 3. Service info — for requests about making things persistent / boot / daemon
    persistence_keywords = ("default", "boot", "startup", "persist", "always",
                             "every time", "on start", "daemon", "service", "enable")
    if any(k in req for k in persistence_keywords):
        # Find the most relevant service from active services or available binaries
        for svc in host_facts.active_services:
            # Match service name words against request
            svc_words = svc.replace("-", " ").replace("_", " ").split()
            if any(w in req for w in svc_words if len(w) > 3):
                _inject("get_service_info", {"service_name": svc})

    return prefetched


def create_plan_generation_request(
    nl_request: str, host_facts: HostFacts
) -> tuple[list[dict], list[dict[str, Any]], bool]:
    """Build messages, tools, and whether pre-fetch data is included.

    Returns (messages, tools, has_prefetch).
    When has_prefetch=True, the caller should force emit_plan immediately.
    """
    emit_plan_tool = {
        "type": "function",
        "function": {
            "name": "emit_plan",
            "description": "Generate a structured execution plan",
            "parameters": Plan.model_json_schema(),
        },
    }
    tools = list(LOOKUP_TOOLS) + [emit_plan_tool]

    host_info = _build_host_context(host_facts)
    prefetched = _prefetch_context(nl_request, host_facts)

    messages: list[dict] = [
        {"role": "system", "content": get_system_prompt()},
    ]
    messages.extend(get_few_shot_examples())
    messages.extend([
        {"role": "user", "content": nl_request},
        {"role": "system", "content": (
            f"Host system information:\n{host_info}\n\n"
            "- Only use binaries listed under 'Available binaries'.\n"
            "- Prefer tools matching active services for the task domain.\n"
            "- If you need a tool that is NOT available, add an install "
            "action FIRST using the listed package manager.\n"
            "- For systemd changes: after writing any unit/drop-in file, "
            "always add a systemctl daemon-reload action."
        )},
    ])

    has_prefetch = bool(prefetched)
    if prefetched:
        messages.extend(prefetched)
        messages.append({
            "role": "system",
            "content": "The above lookup results have been pre-fetched. "
                       "Use them directly. Call emit_plan now.",
        })

    return messages, tools, has_prefetch


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_llm_structured(
    messages: list[dict[str, str]],
    tools: list[dict[str, Any]],
    function_name: str,
    result_model: Type[T],
) -> T:
    """Single-shot forced call (backward compat for adaptive_planner)."""
    client = OpenAIClient()
    return client.call_structured(messages, tools, function_name, result_model)


def call_llm_with_lookups(
    messages: list[dict],
    tools: list[dict[str, Any]],
    host_facts: HostFacts,
    result_model: Type[T],
    has_prefetch: bool = False,
) -> T:
    """Call the LLM with optional pre-fetched context.

    When has_prefetch=True, forces a single API call (no exploration).
    """
    client = OpenAIClient()
    return client.call_with_lookups(
        messages, tools, host_facts, result_model,
        has_prefetch=has_prefetch,
    )


def replan_after_failure(
    original_request: str,
    host_facts: HostFacts,
    failed_plan: "Plan",
    errors: list[str],
) -> "Plan":
    """Ask the LLM for a corrective plan after execution failure.

    Sends the original request, the failed plan's actions with their errors,
    and asks for a new plan that avoids the same mistakes.
    """
    from src.schema import Plan  # avoid circular at module level

    failure_report = []
    for i, action in enumerate(failed_plan.actions, 1):
        action_desc = action.description or str(action.type)
        if hasattr(action, "argv") and action.argv:
            action_desc = f"{action.type}: {' '.join(action.argv)}"
        elif hasattr(action, "path"):
            action_desc = f"{action.type}: {action.path}"
        failure_report.append(f"  {i}. {action_desc}")
    failure_report.append("")
    failure_report.append("Errors:")
    for err in errors:
        failure_report.append(f"  - {err}")

    messages, tools, has_prefetch = create_plan_generation_request(
        original_request, host_facts
    )

    messages.append({
        "role": "system",
        "content": (
            "The previous plan for this request FAILED during execution.\n\n"
            "Previous actions:\n"
            + "\n".join(failure_report) + "\n\n"
            "Generate a CORRECTED plan that avoids the same errors. "
            "Do NOT repeat failed approaches — try an alternative method. "
            "Call emit_plan now."
        ),
    })

    client = OpenAIClient()
    return client.call_with_lookups(
        messages, tools, host_facts, Plan,
        has_prefetch=True,  # force single call
    )


def format_dict_for_prompt(data: dict) -> str:
    """Format a dictionary for inclusion in LLM prompts."""
    lines = []
    for key, value in data.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}: {', '.join(str(v) for v in value[:10])}")
                if len(value) > 10:
                    lines.append(f"  ... and {len(value) - 10} more")
            else:
                lines.append(f"{key}: (none)")
        elif isinstance(value, dict):
            lines.append(f"{key}: {json.dumps(value)}")
        elif value is not None:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)
