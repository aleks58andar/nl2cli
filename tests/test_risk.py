"""Tests for local risk scoring and the confirmation gate."""

from pathlib import Path

import pytest

from src.risk import compute_risk_score, needs_confirmation, one_line_summary

from .conftest import edit, make_plan, restart, shell

HOME = str(Path.home())


class TestComputeRiskScore:
    def test_empty_plan_scores_zero(self):
        assert compute_risk_score(make_plan()) == 0

    def test_harmless_home_command_scores_zero(self):
        assert compute_risk_score(make_plan(shell(f"ls {HOME}"))) == 0

    def test_sudo_adds_twenty_five(self):
        assert compute_risk_score(make_plan(shell(f"ls {HOME}", sudo=True))) == 25

    def test_path_outside_home_adds_fifteen(self):
        assert compute_risk_score(make_plan(shell("ls /etc"))) == 15

    def test_delete_binary_adds_thirty(self):
        # rm + a path outside $HOME
        assert compute_risk_score(make_plan(shell("rm /etc/hosts"))) == 45

    def test_path_penalty_counted_once_per_action(self):
        assert compute_risk_score(make_plan(shell("ls /etc /var /usr"))) == 15

    def test_edit_outside_home_adds_fifteen(self):
        assert compute_risk_score(make_plan(edit("/etc/hosts"))) == 15

    def test_edit_inside_home_is_free(self):
        assert compute_risk_score(make_plan(edit(f"{HOME}/.bashrc"))) == 0

    def test_more_than_three_actions_adds_ten(self):
        plan = make_plan(*[shell(f"echo {i}") for i in range(4)])
        assert compute_risk_score(plan) == 10

    def test_three_actions_do_not_trigger_the_bonus(self):
        plan = make_plan(*[shell(f"echo {i}") for i in range(3)])
        assert compute_risk_score(plan) == 0

    def test_score_is_capped_at_one_hundred(self):
        plan = make_plan(*[shell("rm /etc/hosts", sudo=True) for _ in range(10)])
        assert compute_risk_score(plan) == 100

    def test_restart_service_alone_is_not_scored(self):
        assert compute_risk_score(make_plan(restart("ssh"))) == 0


class TestNeedsConfirmation:
    def test_never_mode_always_skips(self):
        plan = make_plan(shell("rm /etc/hosts", sudo=True))
        must, reasons = needs_confirmation(plan, confirm_mode="never")
        assert must is False
        assert reasons == []

    def test_always_mode_always_confirms(self):
        must, reasons = needs_confirmation(make_plan(), confirm_mode="always")
        assert must is True
        assert reasons == ["confirm_mode is 'always'"]

    def test_auto_mode_passes_harmless_plan(self):
        must, reasons = needs_confirmation(make_plan(shell(f"ls {HOME}")))
        assert must is False
        assert reasons == []

    def test_auto_mode_flags_sudo(self):
        must, reasons = needs_confirmation(make_plan(shell(f"ls {HOME}", sudo=True)))
        assert must is True
        assert any("Requires sudo" in r for r in reasons)

    def test_auto_mode_flags_path_outside_home(self):
        must, reasons = needs_confirmation(make_plan(edit("/etc/hosts")))
        assert must is True
        assert any("outside $HOME" in r for r in reasons)

    def test_auto_mode_flags_deletion(self):
        must, reasons = needs_confirmation(make_plan(shell(f"rm {HOME}/tmpfile")))
        assert must is True
        assert any("Deletion command: rm" in r for r in reasons)

    def test_auto_mode_flags_long_plans(self):
        plan = make_plan(*[shell(f"echo {i}") for i in range(4)])
        must, reasons = needs_confirmation(plan)
        assert must is True
        assert any(">3" in r for r in reasons)

    def test_reasons_are_deduplicated_per_category(self):
        """Each category contributes at most one reason, however many actions hit it."""
        plan = make_plan(
            shell("ls /etc", sudo=True),
            shell("cat /var/log/syslog", sudo=True),
        )
        _, reasons = needs_confirmation(plan)
        assert sum("Requires sudo" in r for r in reasons) == 1


class TestOneLineSummary:
    def test_empty_plan(self):
        assert one_line_summary(make_plan()) == "(empty plan)"

    def test_joins_descriptions(self):
        plan = make_plan(shell("ls"), shell("pwd"))
        assert one_line_summary(plan) == "ls; pwd"

    def test_truncates_after_three_actions(self):
        plan = make_plan(*[shell(f"echo {i}") for i in range(5)])
        summary = one_line_summary(plan)
        assert summary.startswith("echo 0; echo 1; echo 2")
        assert summary.endswith("(+2 more)")
