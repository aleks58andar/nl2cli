"""Tests for host compatibility checking and plan enrichment.

These functions used to live in the planner; they moved to validation so
that validation no longer imports from planning.
"""

import pytest

from src.core.schema import InitSystem
from src.validation.host_compat import (
    enhance_plan_with_context,
    validate_plan_against_host,
)

from .conftest import edit, make_plan as build_plan, restart, shell


class TestValidatePlanAgainstHost:
    def test_a_compatible_plan_passes(self, host_facts):
        plan = build_plan(shell("ls /etc"), init_system=InitSystem.systemd)
        ok, issues = validate_plan_against_host(plan, host_facts)
        assert ok is True
        assert issues == []

    def test_init_system_mismatch_is_reported(self, sysv_host_facts):
        plan = build_plan(shell("ls"), init_system=InitSystem.systemd)
        ok, issues = validate_plan_against_host(plan, sysv_host_facts)
        assert ok is False
        assert any("host uses sysv" in i for i in issues)

    def test_unknown_init_system_is_not_a_mismatch(self, host_facts):
        plan = build_plan(shell("ls"), init_system=InitSystem.unknown)
        ok, _ = validate_plan_against_host(plan, host_facts)
        assert ok is True

    def test_missing_binary_is_reported(self, host_facts):
        plan = build_plan(shell("frobnicate"), init_system=InitSystem.systemd)
        ok, issues = validate_plan_against_host(plan, host_facts)
        assert ok is False
        assert any("frobnicate' not found" in i for i in issues)

    def test_restart_requires_systemctl_on_systemd(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.available_binaries = ["ls"]
        _, issues = validate_plan_against_host(
            build_plan(restart("ssh"), init_system=InitSystem.systemd), facts
        )
        assert any("systemctl" in i for i in issues)

    def test_restart_requires_service_on_sysv(self, sysv_host_facts):
        facts = sysv_host_facts.model_copy(deep=True)
        facts.available_binaries = ["ls"]
        _, issues = validate_plan_against_host(
            build_plan(restart("ssh"), init_system=InitSystem.sysv), facts
        )
        assert any("'service' not found" in i for i in issues)

    def test_each_missing_binary_is_reported_once(self, host_facts):
        plan = build_plan(
            shell("frobnicate a"), shell("frobnicate b"), init_system=InitSystem.systemd
        )
        _, issues = validate_plan_against_host(plan, host_facts)
        assert sum("frobnicate" in i for i in issues) == 1

    def test_known_parent_directory_passes(self, host_facts):
        plan = build_plan(edit("/etc/ssh/sshd_config"), init_system=InitSystem.systemd)
        ok, issues = validate_plan_against_host(plan, host_facts)
        assert ok is True

    def test_unknown_parent_directory_is_flagged(self, host_facts):
        plan = build_plan(edit("/nowhere/at/all.conf"), init_system=InitSystem.systemd)
        ok, issues = validate_plan_against_host(plan, host_facts)
        assert ok is False
        assert any("Parent directory" in i for i in issues)

    def test_virtual_filesystems_skip_the_parent_check(self, host_facts):
        """/sys, /proc and /dev always exist — never flag their parents."""
        for path in [
            "/sys/class/leds/kbd/brightness",
            "/proc/sys/vm/swappiness",
            "/dev/null",
        ]:
            plan = build_plan(edit(path), init_system=InitSystem.systemd)
            ok, issues = validate_plan_against_host(plan, host_facts)
            assert ok is True, f"{path} was flagged: {issues}"


class TestEnhancePlanWithContext:
    def test_unknown_init_system_is_filled_in(self, host_facts):
        plan = build_plan(shell("ls"), init_system=InitSystem.unknown)
        assert enhance_plan_with_context(plan, host_facts).init_system == "systemd"

    def test_a_known_init_system_is_left_alone(self, sysv_host_facts):
        plan = build_plan(shell("ls"), init_system=InitSystem.systemd)
        assert enhance_plan_with_context(plan, sysv_host_facts).init_system == "systemd"

    def test_missing_distro_hint_is_filled_in(self, host_facts):
        plan = build_plan(shell("ls"))
        assert enhance_plan_with_context(plan, host_facts).distro_hint == "pop 22.04"

    def test_an_existing_distro_hint_is_preserved(self, host_facts):
        plan = build_plan(shell("ls"), distro_hint="custom")
        assert enhance_plan_with_context(plan, host_facts).distro_hint == "custom"

    def test_service_descriptions_mention_systemd(self, host_facts):
        plan = build_plan(restart("ssh"))
        enhanced = enhance_plan_with_context(plan, host_facts)
        assert "systemd" in enhanced.actions[0].description

    def test_service_descriptions_mention_sysv(self, sysv_host_facts):
        plan = build_plan(restart("ssh"))
        enhanced = enhance_plan_with_context(plan, sysv_host_facts)
        assert "SysV" in enhanced.actions[0].description

    def test_enhancement_is_in_place(self, host_facts):
        plan = build_plan(shell("ls"))
        assert enhance_plan_with_context(plan, host_facts) is plan
