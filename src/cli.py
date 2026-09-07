"""Simple command-line interface for nl2cli."""

import sys

from rich.console import Console
from rich.prompt import Confirm

from src.config import get_config
from src.executor import run_plan
from src.adaptive_planner import adaptive_plan_generation
from src.renderer import render_rich_preview
from src.utils import gather_host_facts
from src.validators import enhance_and_validate_plan, has_critical_issues, format_validation_issues
from src.risk import needs_confirmation, one_line_summary, compute_risk_score
from src.audit import write_audit_entry, format_action_result

console = Console()


def _print_usage() -> None:
    console.print("[bold]nl2cli - Natural Language to CLI Commands[/bold]")
    console.print("\n[bold]Usage:[/bold]")
    console.print("  nl2cli [OPTIONS] -- your request here")
    console.print("  nl2cli [OPTIONS] your request here  (legacy format)")
    console.print("\n[bold]Examples:[/bold]")
    console.print("  nl2cli -- enable bluetooth")
    console.print("  nl2cli --dry-run -- install docker")
    console.print("  nl2cli --apply -- change ssh port to 2222")
    console.print("\n[bold]Options:[/bold]")
    console.print("  --dry-run, -d      Show what would be done without executing")
    console.print("  --apply, -a        Execute without confirmation (overrides risk gate)")
    console.print("  --verbose, -v      Show detailed output")
    console.print("  --no-adaptive      Disable adaptive planning")
    console.print("  --no-auto-install  Don't auto-install missing tools")
    console.print("  --version          Show version")
    console.print("  --help, -h         Show this help")


def _parse_option(arg: str, flags: dict) -> bool:
    """Parse a single option flag. Returns True if recognized."""
    if arg in ("--dry-run", "-d"):
        flags["dry_run"] = True
    elif arg in ("--apply", "-a"):
        flags["apply"] = True
    elif arg in ("--verbose", "-v"):
        flags["verbose"] = True
    elif arg == "--no-adaptive":
        flags["adaptive"] = False
    elif arg == "--no-auto-install":
        flags["auto_install"] = False
    elif arg == "--version":
        console.print("nl2cli version 0.1.0")
        sys.exit(0)
    elif arg in ("--help", "-h"):
        _print_usage()
        sys.exit(0)
    elif arg.startswith("-"):
        console.print(f"[red]Unknown option: {arg}[/red]")
        sys.exit(1)
    else:
        return False
    return True


def _write_audit(request, plan, issues, *, confirmation_required, confirmation_granted, action_results):
    try:
        write_audit_entry(
            transcript=request,
            plan=plan.model_dump(mode="json"),
            validator_findings=[i.model_dump() for i in issues],
            confirmation_required=confirmation_required,
            confirmation_granted=confirmation_granted,
            action_results=action_results,
        )
    except Exception:
        pass


def main() -> None:
    """Entry point for the CLI application."""
    args = sys.argv[1:]

    if not args:
        _print_usage()
        sys.exit(1)

    # ---- flag defaults ----
    flags = {
        "dry_run": False,
        "apply": False,
        "verbose": False,
        "adaptive": True,
        "auto_install": False,  # PoC: never auto-install by default
    }

    # ---- split on -- or parse legacy ----
    separator_index = None
    try:
        separator_index = args.index("--")
    except ValueError:
        pass

    if separator_index is not None:
        for arg in args[:separator_index]:
            _parse_option(arg, flags)
        request_args = args[separator_index + 1:]
        if not request_args:
            console.print("[red]Error: No request provided after --[/red]")
            sys.exit(1)
        request = " ".join(request_args)
    else:
        request_parts: list[str] = []
        for arg in args:
            if not _parse_option(arg, flags):
                request_parts.append(arg)
        if not request_parts:
            console.print("[red]Error: No request provided[/red]")
            sys.exit(1)
        request = " ".join(request_parts)

    dry_run = flags["dry_run"]
    apply = flags["apply"]
    verbose = flags["verbose"]
    adaptive = flags["adaptive"]
    auto_install = flags["auto_install"]

    try:
        _run(request, dry_run=dry_run, apply=apply, verbose=verbose,
             adaptive=adaptive, auto_install=auto_install)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(130)
    except Exception as e:
        if verbose:
            console.print_exception()
        else:
            console.print(f"[red]Unexpected error: {e}[/red]")
        sys.exit(1)


def _run(
    request: str,
    *,
    dry_run: bool,
    apply: bool,
    verbose: bool,
    adaptive: bool,
    auto_install: bool,
) -> None:
    config = get_config()

    if not config.api_key:
        console.print("[red]Error: OpenAI API key not found[/red]")
        console.print("Set OPENAI_API_KEY environment variable")
        sys.exit(1)

    if config.confirm_mode == "never":
        console.print(
            "[yellow]WARNING: confirm_mode='never' — high-risk actions will "
            "execute without confirmation[/yellow]"
        )

    # ---- gather facts ----
    console.print("[dim]Analyzing system...[/dim]")
    host_facts = gather_host_facts()
    if verbose:
        console.print(
            f"[dim]Detected: {host_facts.distro_id} "
            f"{host_facts.distro_version} with {host_facts.init_system}[/dim]"
        )

    # ---- generate plan ----
    console.print("[dim]Generating plan...[/dim]")
    if adaptive:
        plan, adaptation_messages = adaptive_plan_generation(
            request, host_facts, auto_install=auto_install
        )
        if adaptation_messages:
            console.print()
            for msg in adaptation_messages:
                console.print(f"[blue]{msg}[/blue]")
    else:
        from src.planner import make_plan
        plan = make_plan(request, host_facts)

    # ---- validate ----
    enhanced_plan, issues = enhance_and_validate_plan(plan, host_facts)

    if has_critical_issues(issues):
        console.print("[red]Plan validation failed with critical errors:[/red]")
        console.print(format_validation_issues(issues))
        console.print("\n[yellow]Plan cannot be executed due to safety concerns.[/yellow]")
        sys.exit(3)

    warnings = [iss for iss in issues if iss.severity == "warning"]
    if warnings and verbose:
        console.print("[yellow]Plan validation warnings:[/yellow]")
        console.print(format_validation_issues(warnings))
        console.print()

    # ---- display preview ----
    render_rich_preview(enhanced_plan, host_facts)

    if dry_run:
        console.print("\n[yellow]Dry run mode — no changes will be made[/yellow]")
        return

    if not enhanced_plan.actions:
        console.print("[yellow]No actions to execute[/yellow]")
        return

    # ---- risk gate / confirmation ----
    confirm_mode = "never" if apply else config.confirm_mode
    must_confirm, reasons = needs_confirmation(enhanced_plan, confirm_mode)
    risk = compute_risk_score(enhanced_plan)
    confirmation_granted: bool | None = None

    if must_confirm:
        console.print()
        summary = one_line_summary(enhanced_plan)
        console.print(f"[bold yellow]About to:[/bold yellow] {summary}")
        for r in reasons:
            console.print(f"  [yellow]• {r}[/yellow]")
        console.print(f"  [dim]risk score: {risk}/100[/dim]")
        console.print()
        proceed = Confirm.ask("[bold]Confirm execution?[/bold]", default=False)
        console.print()
        confirmation_granted = proceed
        if not proceed:
            console.print("Cancelled.")
            _write_audit(
                request, enhanced_plan, issues,
                confirmation_required=True,
                confirmation_granted=False,
                action_results=[],
            )
            return

    # ---- execute ----
    console.print()
    results = run_plan(
        enhanced_plan,
        host_facts,
        verbose=verbose,
        action_timeout=config.action_timeout,
    )

    # ---- audit log ----
    action_results_for_log = []
    for action, result in zip(enhanced_plan.actions, results):
        action_results_for_log.append(format_action_result(
            description=action.description,
            returncode=result.error_code,
            duration_ms=result.duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
        ))

    _write_audit(
        request, enhanced_plan, issues,
        confirmation_required=must_confirm,
        confirmation_granted=confirmation_granted,
        action_results=action_results_for_log,
    )

    # ---- exit code ----
    if any(not r.ok for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
