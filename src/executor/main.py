"""Main execution engine for running plans."""

import time

from rich.console import Console

from src.schema import Plan, HostFacts, Action, EditFileAction, ShellAction, RestartServiceAction
from src.executor.base import ActionRunner, Result
from src.executor.filesystem import EditFileRunner
from src.executor.shell import ShellRunner
from src.executor.services import get_service_runner
from src.sudo_manager import SudoManager

console = Console(stderr=True)
stdout_console = Console()


def get_action_runner(action: Action, host_facts: HostFacts, sudo_manager: SudoManager) -> ActionRunner:
    """Get the appropriate runner for an action."""
    if isinstance(action, EditFileAction):
        return EditFileRunner(action, sudo_manager)
    elif isinstance(action, ShellAction):
        return ShellRunner(action, sudo_manager)
    elif isinstance(action, RestartServiceAction):
        return get_service_runner(action, host_facts.init_system)
    else:
        raise ValueError(f"Unknown action type: {type(action)}")


def run_plan(
    plan: Plan,
    host_facts: HostFacts,
    verbose: bool = False,
    dry_run: bool = False,
    action_timeout: int = 30,
) -> list[Result]:
    """Execute a complete plan with per-action timeout and KeyboardInterrupt handling."""
    results: list[Result] = []

    if not plan.actions:
        console.print("[yellow]Plan contains no actions to execute[/yellow]")
        return results

    console.print(f"[bold]Plan:[/bold] {plan.user_request}")
    if dry_run:
        console.print("DRY RUN MODE - No changes will be made")

    sudo_manager = SudoManager()
    if not dry_run and not sudo_manager.authenticate_if_needed(plan.actions):
        console.print("Execution aborted due to authentication failure")
        return results

    for i, action in enumerate(plan.actions, 1):
        try:
            runner = get_action_runner(action, host_facts, sudo_manager)

            if dry_run:
                result = Result(
                    ok=True,
                    stdout=runner.dry_run_description(),
                    changed=False,
                )
                status = "SKIP"
            else:
                t0 = time.monotonic()
                result = runner.run(timeout=action_timeout)
                elapsed = time.monotonic() - t0
                result.duration_ms = int(elapsed * 1000)
                status = "OK" if result.ok else "FAIL"

            results.append(result)

            if status == "OK":
                fmt = "[green][OK][/green]"
            elif status == "FAIL":
                fmt = "[red][FAIL][/red]"
            else:
                fmt = "[yellow][SKIP][/yellow]"
            console.print(f"[bold]Action:[/bold] {action.description} {fmt} [{i}/{len(plan.actions)}]")

            if verbose and result.stdout:
                console.print(f"  Output: {result.stdout.strip()}")

            if not result.ok and not dry_run:
                console.print(f"Error: action failed — {action.description}")
                break

        except KeyboardInterrupt:
            results.append(Result(
                ok=False, stderr="Cancelled by user", error_code=130
            ))
            console.print(f"\n[bold]Action:[/bold] {action.description} [red][CANCEL][/red] [{i}/{len(plan.actions)}]")
            break

        except Exception as e:
            error_result = Result(
                ok=False, stderr=f"Unexpected error: {e}", error_code=1
            )
            results.append(error_result)
            console.print(f"[bold]Action:[/bold] {action.description} [red][FAIL][/red] [{i}/{len(plan.actions)}]")
            console.print(f"Error: action failed — {action.description}")
            if not dry_run:
                break

    console.print()
    _show_execution_summary(results, dry_run)
    return results


def _show_execution_summary(results: list[Result], dry_run: bool) -> None:
    if not results:
        return
    successful = sum(1 for r in results if r.ok)
    failed = len(results) - successful
    if failed == 0 and not dry_run:
        stdout_console.print("[bold]Success:[/bold] All actions completed")
