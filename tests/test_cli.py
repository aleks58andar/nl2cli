"""Tests for CLI argument parsing and the end-to-end control flow.

`_run` is exercised with the planner, executor and confirmation prompt all
mocked — what matters here is which gates fire and what exit codes result.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import cli
from src.executor.base import Result
from src.schema import ValidationIssue

from .conftest import make_plan, shell


def parse(argv: list[str]) -> dict:
    """Run main() with argv, capturing the flags handed to _run."""
    captured = {}

    def fake_run(request, **flags):
        captured["request"] = request
        captured.update(flags)

    with patch.object(sys, "argv", ["nl2cli"] + argv), patch.object(
        cli, "_run", side_effect=fake_run
    ):
        cli.main()
    return captured


class TestArgumentParsing:
    def test_request_after_the_separator(self):
        assert parse(["--", "enable", "bluetooth"])["request"] == "enable bluetooth"

    def test_legacy_form_without_a_separator(self):
        assert parse(["enable", "bluetooth"])["request"] == "enable bluetooth"

    def test_defaults(self):
        flags = parse(["--", "do a thing"])
        assert flags["dry_run"] is False
        assert flags["apply"] is False
        assert flags["verbose"] is False
        assert flags["adaptive"] is True
        assert flags["auto_install"] is False

    @pytest.mark.parametrize("flag", ["--dry-run", "-d"])
    def test_dry_run_flag(self, flag):
        assert parse([flag, "--", "x"])["dry_run"] is True

    @pytest.mark.parametrize("flag", ["--apply", "-a"])
    def test_apply_flag(self, flag):
        assert parse([flag, "--", "x"])["apply"] is True

    @pytest.mark.parametrize("flag", ["--verbose", "-v"])
    def test_verbose_flag(self, flag):
        assert parse([flag, "--", "x"])["verbose"] is True

    def test_no_adaptive_flag(self):
        assert parse(["--no-adaptive", "--", "x"])["adaptive"] is False

    def test_flags_can_be_combined(self):
        flags = parse(["-d", "-v", "--", "x"])
        assert flags["dry_run"] and flags["verbose"]

    def test_flags_are_recognised_in_the_legacy_form(self):
        flags = parse(["--dry-run", "enable", "bluetooth"])
        assert flags["dry_run"] is True
        assert flags["request"] == "enable bluetooth"

    def test_words_after_the_separator_are_never_read_as_flags(self):
        """A request may legitimately contain something flag-shaped."""
        assert parse(["--", "explain", "--dry-run"])["request"] == "explain --dry-run"


class TestArgumentErrors:
    def _exit_code(self, argv: list[str]) -> int:
        with patch.object(sys, "argv", ["nl2cli"] + argv), patch.object(cli, "_run"):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        return exc.value.code

    def test_no_arguments_exits_one(self):
        assert self._exit_code([]) == 1

    def test_nothing_after_the_separator_exits_one(self):
        assert self._exit_code(["--"]) == 1

    def test_only_flags_exits_one(self):
        assert self._exit_code(["--dry-run"]) == 1

    def test_unknown_option_exits_one(self):
        assert self._exit_code(["--frobnicate", "--", "x"]) == 1

    def test_version_exits_zero(self):
        assert self._exit_code(["--version"]) == 0

    def test_help_exits_zero(self):
        assert self._exit_code(["--help"]) == 0


class TestMainErrorHandling:
    def test_keyboard_interrupt_exits_130(self):
        with patch.object(sys, "argv", ["nl2cli", "--", "x"]), patch.object(
            cli, "_run", side_effect=KeyboardInterrupt
        ):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 130

    def test_unexpected_error_exits_one(self):
        with patch.object(sys, "argv", ["nl2cli", "--", "x"]), patch.object(
            cli, "_run", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1


@pytest.fixture
def wired(host_facts, monkeypatch):
    """Wire _run's collaborators to controllable doubles."""
    doubles = MagicMock()
    doubles.config.api_key = "sk-test"
    doubles.config.confirm_mode = "auto"
    doubles.config.action_timeout = 30
    doubles.plan = make_plan(shell("ls /etc"), request="list etc")
    doubles.issues = []
    doubles.results = [Result(ok=True, error_code=0, duration_ms=1)]

    monkeypatch.setattr(cli, "get_config", lambda: doubles.config)
    monkeypatch.setattr(cli, "gather_host_facts", lambda: host_facts)
    monkeypatch.setattr(
        cli, "adaptive_plan_generation", lambda *a, **k: (doubles.plan, [])
    )
    monkeypatch.setattr(
        cli, "enhance_and_validate_plan", lambda p, h: (doubles.plan, doubles.issues)
    )
    monkeypatch.setattr(cli, "render_rich_preview", lambda *a, **k: None)
    monkeypatch.setattr(cli, "run_plan", MagicMock(return_value=doubles.results))
    monkeypatch.setattr(cli, "write_audit_entry", MagicMock())
    doubles.run_plan = cli.run_plan
    doubles.write_audit_entry = cli.write_audit_entry
    return doubles


def run_cli(**overrides):
    kwargs = {
        "dry_run": False,
        "apply": False,
        "verbose": False,
        "adaptive": True,
        "auto_install": False,
    }
    kwargs.update(overrides)
    return cli._run("list etc", **kwargs)


class TestRunFlow:
    def test_missing_api_key_exits_one(self, wired):
        wired.config.api_key = None
        with pytest.raises(SystemExit) as exc:
            run_cli()
        assert exc.value.code == 1

    def test_critical_validation_issues_exit_three(self, wired):
        wired.issues = [ValidationIssue(severity="error", message="unsafe")]
        with pytest.raises(SystemExit) as exc:
            run_cli()
        assert exc.value.code == 3
        wired.run_plan.assert_not_called()

    def test_warnings_do_not_block_execution(self, wired):
        wired.issues = [ValidationIssue(severity="warning", message="hmm")]
        with patch("src.cli.Confirm.ask", return_value=True):
            run_cli()
        wired.run_plan.assert_called_once()

    def test_dry_run_stops_before_execution(self, wired):
        run_cli(dry_run=True)
        wired.run_plan.assert_not_called()

    def test_empty_plan_executes_nothing(self, wired):
        wired.plan = make_plan(request="do nothing")
        run_cli()
        wired.run_plan.assert_not_called()

    def test_non_adaptive_mode_uses_the_plain_planner(self, wired):
        with patch("src.planner.make_plan", return_value=wired.plan) as make, patch(
            "src.cli.Confirm.ask", return_value=True
        ):
            run_cli(adaptive=False)
        make.assert_called_once()

    def test_a_failing_action_exits_one(self, wired):
        wired.run_plan.return_value = [Result(ok=False, error_code=1)]
        with patch("src.cli.Confirm.ask", return_value=True):
            with pytest.raises(SystemExit) as exc:
                run_cli()
        assert exc.value.code == 1

    def test_action_timeout_comes_from_config(self, wired):
        wired.config.action_timeout = 90
        with patch("src.cli.Confirm.ask", return_value=True):
            run_cli()
        assert wired.run_plan.call_args.kwargs["action_timeout"] == 90


class TestConfirmationGate:
    def test_declining_cancels_before_execution(self, wired):
        with patch("src.cli.Confirm.ask", return_value=False):
            run_cli()
        wired.run_plan.assert_not_called()

    def test_declining_is_recorded_in_the_audit_log(self, wired):
        with patch("src.cli.Confirm.ask", return_value=False):
            run_cli()
        entry = wired.write_audit_entry.call_args.kwargs
        assert entry["confirmation_required"] is True
        assert entry["confirmation_granted"] is False
        assert entry["action_results"] == []

    def test_accepting_proceeds(self, wired):
        with patch("src.cli.Confirm.ask", return_value=True):
            run_cli()
        wired.run_plan.assert_called_once()

    def test_apply_skips_the_prompt(self, wired):
        with patch("src.cli.Confirm.ask") as ask:
            run_cli(apply=True)
        ask.assert_not_called()
        wired.run_plan.assert_called_once()

    def test_harmless_plans_are_not_gated(self, wired):
        home = Path.home()
        wired.plan = make_plan(shell(f"ls {home}"), request="list")
        with patch("src.cli.Confirm.ask") as ask, patch.object(
            cli, "enhance_and_validate_plan", lambda p, h: (wired.plan, [])
        ):
            run_cli()
        ask.assert_not_called()


class TestAuditLogging:
    def test_a_successful_run_is_logged(self, wired):
        with patch("src.cli.Confirm.ask", return_value=True):
            run_cli()
        entry = wired.write_audit_entry.call_args.kwargs
        assert entry["transcript"] == "list etc"
        assert len(entry["action_results"]) == 1
        assert entry["action_results"][0]["returncode"] == 0

    def test_audit_failures_never_break_the_run(self, wired):
        wired.write_audit_entry.side_effect = OSError("disk full")
        with patch("src.cli.Confirm.ask", return_value=True):
            run_cli()  # must not raise

    def test_validation_findings_are_recorded(self, wired):
        wired.issues = [ValidationIssue(severity="warning", message="hmm")]
        with patch("src.cli.Confirm.ask", return_value=True):
            run_cli()
        findings = wired.write_audit_entry.call_args.kwargs["validator_findings"]
        assert findings[0]["message"] == "hmm"
