"""Tests for plan validation.

Characterisation tests: these lock in the current behaviour of the
validation layer so it can be moved between modules without drift.
"""

import pytest

from src.schema import EditMode, InitSystem, Plan, ValidationIssue
from src.validators import (
    _validate_edit_file_action,
    _validate_plan_structure,
    _validate_shell_action,
    enhance_and_validate_plan,
    format_validation_issues,
    has_critical_issues,
    validate_plan,
)

from .conftest import edit, make_plan, restart, shell


def severities(issues: list[ValidationIssue]) -> set[str]:
    return {i.severity for i in issues}


def messages(issues: list[ValidationIssue]) -> str:
    return "\n".join(i.message for i in issues)


class TestValidatePlan:
    def test_empty_plan_yields_single_warning(self, host_facts):
        issues = validate_plan(make_plan(), host_facts)
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "no actions" in issues[0].message

    def test_clean_plan_has_no_errors(self, host_facts):
        plan = make_plan(shell("ls /etc"), init_system=InitSystem.systemd)
        issues = validate_plan(plan, host_facts)
        assert not has_critical_issues(issues)

    def test_unknown_binary_is_reported_by_both_layers(self, host_facts):
        """A missing binary surfaces twice: host-compat error + action warning.

        The duplication is deliberate — the host-compat pass blocks the plan,
        the per-action pass points at which action is at fault.
        """
        plan = make_plan(shell("frobnicate --all"), init_system=InitSystem.systemd)
        issues = validate_plan(plan, host_facts)

        host_level = [i for i in issues if i.message.startswith("Host compatibility")]
        action_level = [i for i in issues if "not found in system PATH" in i.message]

        assert [i.severity for i in host_level] == ["error"]
        assert [i.severity for i in action_level] == ["warning"]
        assert action_level[0].action_index == 0
        assert has_critical_issues(issues)

    def test_init_system_mismatch_is_an_error(self, sysv_host_facts):
        plan = make_plan(shell("ls"), init_system=InitSystem.systemd)
        issues = validate_plan(plan, sysv_host_facts)
        assert has_critical_issues(issues)
        assert "Host compatibility" in messages(issues)

    def test_risky_action_flagged(self, host_facts):
        plan = make_plan(shell("ls /etc", risky=True), init_system=InitSystem.systemd)
        issues = validate_plan(plan, host_facts)
        assert "marked as risky" in messages(issues)


class TestShellActionValidation:
    def test_shell_glue_rejected_by_default(self, host_facts):
        action = shell("ls")
        action.argv = ["bash", "-c", "echo hi"]
        issues = _validate_shell_action(action, host_facts, 0)
        assert any(i.severity == "error" for i in issues)
        assert "Explicit shell invocation" in messages(issues)

    def test_python_dash_c_rejected(self, host_facts):
        action = shell("ls")
        action.argv = ["python3", "-c", "import os"]
        issues = _validate_shell_action(action, host_facts, 0)
        assert "python -c as shell glue" in messages(issues)

    def test_sudo_in_argv_is_an_error(self, host_facts):
        action = shell("ls")
        action.argv = ["sudo", "ls"]
        issues = _validate_shell_action(action, host_facts, 0)
        assert any(i.severity == "error" for i in issues)
        assert "sudo must not appear" in messages(issues)

    def test_empty_argv_short_circuits(self, host_facts):
        action = shell("ls")
        action.argv = []
        issues = _validate_shell_action(action, host_facts, 0)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "empty argv" in issues[0].message

    def test_known_binary_produces_no_issues(self, host_facts):
        issues = _validate_shell_action(shell("systemctl status ssh"), host_facts, 0)
        assert issues == []

    def test_action_index_is_propagated(self, host_facts):
        issues = _validate_shell_action(shell("frobnicate"), host_facts, 3)
        assert issues and all(i.action_index == 3 for i in issues)


class TestEditFileActionValidation:
    def test_relative_path_warns(self, host_facts, tmp_path):
        action = edit("relative/file.conf")
        issues = _validate_edit_file_action(action, host_facts, 0)
        assert "should be absolute" in messages(issues)

    def test_missing_file_under_standard_dir_is_info_only(self, host_facts):
        action = edit("/etc/definitely-not-here.conf")
        issues = _validate_edit_file_action(action, host_facts, 0)
        assert severities(issues) <= {"info"}

    def test_replace_line_requires_selector_and_after(self, host_facts, tmp_path):
        target = tmp_path / "f.conf"
        target.write_text("Key old\n")
        action = edit(str(target), mode=EditMode.replace_line, selector=None, after=None)
        issues = _validate_edit_file_action(action, host_facts, 0)
        assert "requires a selector" in messages(issues)
        assert "requires 'after' content" in messages(issues)

    def test_ensure_kv_requires_key_and_value(self, host_facts, tmp_path):
        target = tmp_path / "f.conf"
        target.write_text("")
        action = edit(
            str(target), mode=EditMode.ensure_kv, ensure_key=None, ensure_value=None
        )
        issues = _validate_edit_file_action(action, host_facts, 0)
        assert "requires ensure_key" in messages(issues)
        assert "requires ensure_value" in messages(issues)

    def test_insert_requires_after(self, host_facts, tmp_path):
        target = tmp_path / "f.conf"
        target.write_text("")
        action = edit(str(target), mode=EditMode.insert, after=None)
        issues = _validate_edit_file_action(action, host_facts, 0)
        assert "insert mode requires 'after' content" in messages(issues)

    def test_valid_existing_file_has_no_issues(self, host_facts, tmp_path):
        target = tmp_path / "f.conf"
        target.write_text("Key old\n")
        issues = _validate_edit_file_action(edit(str(target)), host_facts, 0)
        assert issues == []


class TestPlanStructure:
    def test_duplicate_file_edits_warn(self):
        plan = make_plan(edit("/etc/hosts"), edit("/etc/hosts"))
        issues = _validate_plan_structure(plan)
        assert "edited multiple times" in messages(issues)

    def test_duplicate_service_restarts_warn(self):
        plan = make_plan(restart("ssh"), restart("ssh"))
        issues = _validate_plan_structure(plan)
        assert "restarted multiple times" in messages(issues)

    def test_high_sudo_ratio_warns(self):
        plan = make_plan(shell("ls", sudo=True), shell("cat /etc/hosts", sudo=True))
        issues = _validate_plan_structure(plan)
        assert "High number of sudo actions" in messages(issues)

    def test_mixed_plan_below_sudo_threshold_is_quiet(self):
        plan = make_plan(shell("ls", sudo=True), shell("echo a"), shell("echo b"))
        issues = _validate_plan_structure(plan)
        assert "High number of sudo actions" not in messages(issues)


class TestEnhanceAndValidate:
    def test_unknown_init_system_is_filled_from_host(self, host_facts):
        plan = make_plan(shell("ls"), init_system=InitSystem.unknown)
        enhanced, _ = enhance_and_validate_plan(plan, host_facts)
        assert enhanced.init_system == InitSystem.systemd

    def test_distro_hint_is_filled_from_host(self, host_facts):
        plan = make_plan(shell("ls"))
        enhanced, _ = enhance_and_validate_plan(plan, host_facts)
        assert enhanced.distro_hint == "pop 22.04"

    def test_returns_the_same_plan_object(self, host_facts):
        plan = make_plan(shell("ls"))
        enhanced, _ = enhance_and_validate_plan(plan, host_facts)
        assert enhanced is plan


class TestIssueReporting:
    def test_has_critical_issues_only_counts_errors(self):
        assert not has_critical_issues([ValidationIssue(severity="warning", message="w")])
        assert not has_critical_issues([ValidationIssue(severity="info", message="i")])
        assert has_critical_issues([ValidationIssue(severity="error", message="e")])

    def test_format_empty(self):
        assert format_validation_issues([]) == "No validation issues found."

    def test_format_includes_action_number_one_indexed(self):
        out = format_validation_issues(
            [ValidationIssue(severity="error", message="boom", action_index=0)]
        )
        assert "boom" in out
        assert "(Action 1)" in out

    def test_format_omits_action_for_plan_level_issues(self):
        out = format_validation_issues([ValidationIssue(severity="warning", message="w")])
        assert "Action" not in out
