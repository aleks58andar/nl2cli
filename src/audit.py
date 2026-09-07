"""Minimal JSONL audit log for every plan execution."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HISTORY_FILE = Path.home() / ".local" / "state" / "nl2cli" / "history.jsonl"
_STDOUT_CLIP = 500


def _clip(text: str, limit: int = _STDOUT_CLIP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def write_audit_entry(
    *,
    transcript: str,
    plan: dict[str, Any],
    validator_findings: list[dict[str, Any]],
    confirmation_required: bool,
    confirmation_granted: bool | None,
    action_results: list[dict[str, Any]],
) -> Path:
    """Append one JSONL line to the history file and return its path."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transcript": transcript,
        "plan": plan,
        "validator_findings": validator_findings,
        "confirmation_required": confirmation_required,
        "confirmation_granted": confirmation_granted,
        "action_results": action_results,
    }
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return HISTORY_FILE


def format_action_result(
    *,
    description: str,
    returncode: int | None,
    duration_ms: int | None,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    """Build a dict suitable for the ``action_results`` list."""
    return {
        "description": description,
        "returncode": returncode,
        "duration_ms": duration_ms,
        "stdout_clip": _clip(stdout),
        "stderr_clip": _clip(stderr),
    }
