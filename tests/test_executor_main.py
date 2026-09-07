"""Tests for runner dispatch, service-runner selection, and plan orchestration.

`run_plan` is tested with every runner mocked — the point is the control
flow (dry run, stop-on-failure, sudo gate, cancellation), not the commands.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.execution.base import Result
from src.execution.filesystem import EditFileRunner
from src.execution.runner import get_action_runner, run_plan
from src.execution.services import UpstartServiceRunner, get_service_runner
from src.execution.shell import (
    ServiceRunner,
    ShellRunner,
    SysVServiceRunner,
    SystemctlRunner,
)
from src.core.schema import InitSystem

from .conftest import edit, make_plan, restart, shell


class TestGetActionRunner:
    def test_shell_action_maps_to_shell_runner(self, host_facts):
        runner = get_action_runner(shell("ls"), host_facts, MagicMock())
        assert isinstance(runner, ShellRunner)

    def test_edit_action_maps_to_file_runner(self, host_facts):
        runner = get_action_runner(edit("/etc/hosts"), host_facts, MagicMock())
        assert isinstance(runner, EditFileRunner)

    def test_restart_action_maps_to_a_service_runner(self, host_facts):
        runner = get_action_runner(restart("ssh"), host_facts, MagicMock())
        assert isinstance(runner, SystemctlRunner)

    def test_sudo_manager_is_threaded_through(self, host_facts):
        manager = MagicMock()
        assert get_action_runner(shell("ls"), host_facts, manager).sudo_manager is manager

    def test_unknown_action_type_raises(self, host_facts):
        with pytest.raises(ValueError, match="Unknown action type"):
            get_action_runner(object(), host_facts, MagicMock())


class TestGetServiceRunner:
    def test_systemd_selects_systemctl(self):
        runner = get_service_runner(restart("ssh"), InitSystem.systemd)
        assert isinstance(runner, SystemctlRunner)

    def test_sysv_selects_the_init_script_runner(self):
        runner = get_service_runner(restart("ssh"), InitSystem.sysv)
        assert isinstance(runner, SysVServiceRunner)

    def test_upstart_prefers_the_service_command_when_present(self):
        with patch("src.execution.services.which", return_value="/usr/sbin/service"):
            runner = get_service_runner(restart("ssh"), InitSystem.upstart)
        assert isinstance(runner, ServiceRunner)

    def test_upstart_falls_back_to_initctl(self):
        with patch("src.execution.services.which", return_value=None):
            runner = get_service_runner(restart("ssh"), InitSystem.upstart)
        assert isinstance(runner, UpstartServiceRunner)

    def test_unknown_init_system_prefers_service_then_systemctl(self):
        with patch("src.execution.services.which", side_effect=lambda b: b == "systemctl"):
            runner = get_service_runner(restart("ssh"), InitSystem.unknown)
        assert isinstance(runner, SystemctlRunner)

    def test_unknown_init_system_last_resort_is_sysv(self):
        with patch("src.execution.services.which", return_value=None):
            runner = get_service_runner(restart("ssh"), InitSystem.unknown)
        assert isinstance(runner, SysVServiceRunner)

    def test_service_name_is_carried_through(self):
        assert get_service_runner(restart("nginx"), InitSystem.systemd).service_name == (
            "nginx"
        )


class TestUpstartServiceRunner:
    def test_requires_sudo(self):
        assert UpstartServiceRunner("ssh").requires_sudo is True

    def test_missing_initctl_fails_cleanly(self):
        with patch("src.execution.services.which", return_value=None):
            result = UpstartServiceRunner("ssh").run()
        assert result.ok is False
        assert "initctl) is not available" in result.stderr

    def test_missing_configuration_fails_cleanly(self):
        with patch("src.execution.services.which", return_value="/sbin/initctl"):
            result = UpstartServiceRunner("definitely-not-a-service").run()
        assert result.ok is False
        assert "configuration not found" in result.stderr

    def test_dry_run_description(self):
        assert UpstartServiceRunner("ssh").dry_run_description() == (
            "Would execute: sudo initctl restart ssh"
        )


@pytest.fixture
def no_sudo_prompt():
    """Stub SudoManager so no test can ever trigger a real sudo prompt."""
    with patch("src.execution.runner.SudoManager") as manager:
        manager.return_value.authenticate_if_needed.return_value = True
        yield manager.return_value


def stub_runner(*results: Result) -> MagicMock:
    runner = MagicMock()
    runner.run.side_effect = list(results)
    runner.dry_run_description.return_value = "Would execute: something"
    return runner


class TestRunPlan:
    def test_empty_plan_runs_nothing(self, host_facts, no_sudo_prompt):
        assert run_plan(make_plan(), host_facts) == []

    def test_every_action_runs_on_success(self, host_facts, no_sudo_prompt):
        plan = make_plan(shell("echo a"), shell("echo b"))
        runner = stub_runner(Result(ok=True), Result(ok=True))
        with patch("src.execution.runner.get_action_runner", return_value=runner):
            results = run_plan(plan, host_facts)

        assert len(results) == 2
        assert all(r.ok for r in results)
        assert runner.run.call_count == 2

    def test_execution_stops_at_the_first_failure(self, host_facts, no_sudo_prompt):
        plan = make_plan(shell("echo a"), shell("echo b"), shell("echo c"))
        runner = stub_runner(Result(ok=False, error_code=1), Result(ok=True))
        with patch("src.execution.runner.get_action_runner", return_value=runner):
            results = run_plan(plan, host_facts)

        assert len(results) == 1
        assert runner.run.call_count == 1

    def test_duration_is_recorded_per_action(self, host_facts, no_sudo_prompt):
        runner = stub_runner(Result(ok=True))
        with patch("src.execution.runner.get_action_runner", return_value=runner):
            results = run_plan(make_plan(shell("echo a")), host_facts)
        assert results[0].duration_ms is not None
        assert results[0].duration_ms >= 0

    def test_action_timeout_is_passed_to_the_runner(self, host_facts, no_sudo_prompt):
        runner = stub_runner(Result(ok=True))
        with patch("src.execution.runner.get_action_runner", return_value=runner):
            run_plan(make_plan(shell("echo a")), host_facts, action_timeout=7)
        assert runner.run.call_args.kwargs["timeout"] == 7

    def test_a_raising_runner_is_captured_as_a_failed_result(
        self, host_facts, no_sudo_prompt
    ):
        runner = MagicMock()
        runner.run.side_effect = RuntimeError("boom")
        with patch("src.execution.runner.get_action_runner", return_value=runner):
            results = run_plan(make_plan(shell("echo a")), host_facts)

        assert results[0].ok is False
        assert "Unexpected error: boom" in results[0].stderr

    def test_keyboard_interrupt_is_recorded_as_cancelled(
        self, host_facts, no_sudo_prompt
    ):
        runner = MagicMock()
        runner.run.side_effect = KeyboardInterrupt()
        with patch("src.execution.runner.get_action_runner", return_value=runner):
            results = run_plan(make_plan(shell("echo a"), shell("echo b")), host_facts)

        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].error_code == 130
        assert "Cancelled by user" in results[0].stderr


class TestRunPlanDryRun:
    def test_nothing_is_executed(self, host_facts, no_sudo_prompt):
        runner = stub_runner(Result(ok=True))
        with patch("src.execution.runner.get_action_runner", return_value=runner):
            results = run_plan(make_plan(shell("echo a")), host_facts, dry_run=True)

        runner.run.assert_not_called()
        assert results[0].ok is True
        assert results[0].changed is False
        assert results[0].stdout == "Would execute: something"

    def test_dry_run_never_authenticates(self, host_facts, no_sudo_prompt):
        with patch("src.execution.runner.get_action_runner", return_value=stub_runner()):
            run_plan(make_plan(shell("ls", sudo=True)), host_facts, dry_run=True)
        no_sudo_prompt.authenticate_if_needed.assert_not_called()

    def test_dry_run_continues_past_a_failing_action(self, host_facts, no_sudo_prompt):
        plan = make_plan(shell("echo a"), shell("echo b"))
        runner = MagicMock()
        runner.dry_run_description.side_effect = ["one", "two"]
        with patch("src.execution.runner.get_action_runner", return_value=runner):
            results = run_plan(plan, host_facts, dry_run=True)
        assert [r.stdout for r in results] == ["one", "two"]


class TestRunPlanSudoGate:
    def test_failed_authentication_aborts_before_any_action(self, host_facts):
        with patch("src.execution.runner.SudoManager") as manager:
            manager.return_value.authenticate_if_needed.return_value = False
            with patch("src.execution.runner.get_action_runner") as get_runner:
                results = run_plan(make_plan(shell("ls", sudo=True)), host_facts)

        assert results == []
        get_runner.assert_not_called()

    def test_authentication_is_offered_the_whole_action_list(
        self, host_facts, no_sudo_prompt
    ):
        plan = make_plan(shell("ls", sudo=True), shell("echo a"))
        with patch("src.execution.runner.get_action_runner", return_value=stub_runner(
            Result(ok=True), Result(ok=True)
        )):
            run_plan(plan, host_facts)
        assert no_sudo_prompt.authenticate_if_needed.call_args[0][0] == plan.actions
