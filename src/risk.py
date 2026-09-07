"""Local risk assessment for execution plans."""

from pathlib import Path

from src.schema import Plan, ShellAction, EditFileAction

_DELETE_BINARIES = frozenset({"rm", "rmdir", "shred", "unlink"})


def compute_risk_score(plan: Plan) -> int:
    """Return a risk score in 0-100 for *plan*, computed purely locally."""
    score = 0
    home = str(Path.home())

    for action in plan.actions:
        if action.requires_sudo:
            score += 25

        if isinstance(action, ShellAction):
            if action.argv and action.argv[0] in _DELETE_BINARIES:
                score += 30
            for arg in action.argv:
                if arg.startswith("/") and not arg.startswith(home):
                    score += 15
                    break

        if isinstance(action, EditFileAction):
            if not action.path.startswith(home):
                score += 15

    if len(plan.actions) > 3:
        score += 10

    return min(score, 100)


def needs_confirmation(plan: Plan, confirm_mode: str = "auto") -> tuple[bool, list[str]]:
    """Decide whether confirmation is required before execution.

    Returns ``(must_confirm, reasons)``.
    """
    if confirm_mode == "never":
        return False, []

    if confirm_mode == "always":
        return True, ["confirm_mode is 'always'"]

    # ----- auto mode -----
    reasons: list[str] = []
    home = str(Path.home())

    for action in plan.actions:
        if action.requires_sudo:
            reasons.append(f"Requires sudo: {action.description}")
            break

    for action in plan.actions:
        if isinstance(action, EditFileAction):
            if not action.path.startswith(home):
                reasons.append(f"Touches path outside $HOME: {action.path}")
                break
        if isinstance(action, ShellAction):
            for arg in action.argv:
                if arg.startswith("/") and not arg.startswith(home):
                    reasons.append(f"References path outside $HOME: {arg}")
                    break

    for action in plan.actions:
        if isinstance(action, ShellAction):
            if action.argv and action.argv[0] in _DELETE_BINARIES:
                reasons.append(f"Deletion command: {action.argv[0]}")
                break

    if len(plan.actions) > 3:
        reasons.append(f"Plan has {len(plan.actions)} actions (>3)")

    return len(reasons) > 0, reasons


def one_line_summary(plan: Plan) -> str:
    """Return a short human-readable summary of the plan."""
    n = len(plan.actions)
    if n == 0:
        return "(empty plan)"
    descs = [a.description for a in plan.actions[:3]]
    summary = "; ".join(descs)
    if n > 3:
        summary += f" (+{n - 3} more)"
    return summary
