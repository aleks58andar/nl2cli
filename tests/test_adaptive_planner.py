"""Tests for adaptive planning — missing tools, substitution, install actions."""

from unittest.mock import patch

import pytest

from src.planning.adaptive_planner import (
    ToolAlternatives,
    adaptive_plan_generation,
    create_tool_installation_actions,
    detect_missing_tools,
    find_working_alternative,
    get_package_manager,
    substitute_alternatives_in_plan,
)
from src.planning.planner import PlannerError
from src.core.schema import HostFacts, InitSystem, Plan

from .conftest import edit, make_plan, restart, shell


class TestToolAlternatives:
    def test_known_tool_has_alternatives(self):
        assert "service" in ToolAlternatives.get_alternatives("systemctl")

    def test_unknown_tool_has_no_alternatives(self):
        assert ToolAlternatives.get_alternatives("frobnicate") == []

    def test_can_install_known_tool(self):
        assert ToolAlternatives.can_install("curl") is True

    def test_cannot_install_unknown_tool(self):
        assert ToolAlternatives.can_install("frobnicate") is False

    def test_install_package_is_per_package_manager(self):
        assert ToolAlternatives.get_install_package("nmcli", "apt") == "network-manager"
        assert ToolAlternatives.get_install_package("nmcli", "dnf") == "NetworkManager"

    def test_install_package_none_for_unknown_manager(self):
        assert ToolAlternatives.get_install_package("curl", "brew") is None

    def test_install_package_none_for_uninstallable_tool(self):
        assert ToolAlternatives.get_install_package("frobnicate", "apt") is None


class TestDetectMissingTools:
    def test_available_binary_is_not_missing(self, host_facts):
        assert detect_missing_tools(make_plan(shell("ls /etc")), host_facts) == set()

    def test_unavailable_binary_is_missing(self, host_facts):
        assert detect_missing_tools(make_plan(shell("frobnicate")), host_facts) == {
            "frobnicate"
        }

    def test_restart_service_needs_systemctl_on_systemd(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.available_binaries = ["ls"]
        assert detect_missing_tools(make_plan(restart("ssh")), facts) == {"systemctl"}

    def test_restart_service_needs_service_on_sysv(self, sysv_host_facts):
        facts = sysv_host_facts.model_copy(deep=True)
        facts.available_binaries = ["ls"]
        assert detect_missing_tools(make_plan(restart("ssh")), facts) == {"service"}

    def test_edit_actions_require_no_tools(self, host_facts):
        assert detect_missing_tools(make_plan(edit("/etc/hosts")), host_facts) == set()


class TestFindWorkingAlternative:
    def test_returns_first_available_alternative(self, sysv_host_facts):
        assert find_working_alternative("systemctl", sysv_host_facts) == "service"

    def test_returns_none_when_no_alternative_is_installed(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.available_binaries = ["ls"]
        assert find_working_alternative("systemctl", facts) is None

    def test_returns_none_for_unknown_tool(self, host_facts):
        assert find_working_alternative("frobnicate", host_facts) is None

    def test_respects_alternative_ordering(self, host_facts):
        """The first alternative present in the list wins, not the first installed."""
        facts = host_facts.model_copy(deep=True)
        facts.available_binaries = ["tlp", "powerprofilesctl", "system76-power"]
        assert find_working_alternative("cpupower", facts) == "system76-power"


class TestGetPackageManager:
    def test_prefers_the_pre_detected_value(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.package_manager = "dnf"
        facts.available_binaries = ["apt"]
        assert get_package_manager(facts) == "dnf"

    def test_falls_back_to_binary_sniffing(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.package_manager = None
        facts.available_binaries = ["pacman"]
        assert get_package_manager(facts) == "pacman"

    def test_returns_none_when_nothing_matches(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.package_manager = None
        facts.available_binaries = ["ls"]
        assert get_package_manager(facts) is None


class TestCreateToolInstallationActions:
    def test_apt_emits_update_then_install(self, host_facts):
        actions = create_tool_installation_actions({"curl"}, host_facts)
        assert len(actions) == 2
        assert actions[0].argv == ["apt", "update"]
        assert actions[1].argv == ["apt", "install", "-y", "curl"]
        assert all(a.requires_sudo for a in actions)

    def test_pacman_emits_a_single_noconfirm_install(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.package_manager = "pacman"
        actions = create_tool_installation_actions({"curl"}, facts)
        assert len(actions) == 1
        assert actions[0].argv == ["pacman", "-S", "--noconfirm", "curl"]

    def test_dnf_emits_a_single_install(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.package_manager = "dnf"
        actions = create_tool_installation_actions({"git"}, facts)
        assert len(actions) == 1
        assert actions[0].argv == ["dnf", "install", "-y", "git"]

    def test_no_package_manager_yields_no_actions(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.package_manager = None
        facts.available_binaries = ["ls"]
        assert create_tool_installation_actions({"curl"}, facts) == []

    def test_uninstallable_tools_yield_no_actions(self, host_facts):
        assert create_tool_installation_actions({"frobnicate"}, host_facts) == []

    def test_install_actions_never_embed_sudo(self, host_facts):
        for action in create_tool_installation_actions({"curl", "git"}, host_facts):
            assert "sudo" not in action.argv


class TestSubstituteAlternatives:
    def test_no_substitutions_returns_the_same_plan(self, host_facts):
        plan = make_plan(shell("systemctl status ssh"))
        assert substitute_alternatives_in_plan(plan, host_facts, {}) is plan

    def test_binary_is_swapped_and_noted(self, host_facts):
        plan = make_plan(shell("systemctl restart ssh"))
        out = substitute_alternatives_in_plan(plan, host_facts, {"systemctl": "service"})
        assert out.actions[0].argv == ["service", "restart", "ssh"]
        assert "using service instead of systemctl" in out.actions[0].description

    def test_untouched_actions_are_preserved(self, host_facts):
        plan = make_plan(shell("ls /etc"), edit("/etc/hosts"), restart("ssh"))
        out = substitute_alternatives_in_plan(plan, host_facts, {"systemctl": "service"})
        assert [a.type for a in out.actions] == [a.type for a in plan.actions]

    def test_plan_metadata_is_carried_over(self, host_facts):
        plan = make_plan(
            shell("systemctl restart ssh"),
            request="restart ssh",
            notes="a note",
            init_system=InitSystem.systemd,
        )
        out = substitute_alternatives_in_plan(plan, host_facts, {"systemctl": "service"})
        assert out.user_request == "restart ssh"
        assert out.notes == "a note"
        assert out.init_system == InitSystem.systemd

    def test_sudo_flag_survives_substitution(self, host_facts):
        plan = make_plan(shell("systemctl restart ssh", sudo=True))
        out = substitute_alternatives_in_plan(plan, host_facts, {"systemctl": "service"})
        assert out.actions[0].requires_sudo is True


class TestAdaptivePlanGeneration:
    """The LLM boundary is mocked; only the adaptation logic is under test."""

    @staticmethod
    def _patch_llm(plan: Plan):
        return patch(
            "src.planning.adaptive_planner.call_llm_with_lookups", return_value=plan
        ), patch(
            "src.planning.adaptive_planner.create_plan_generation_request",
            return_value=([], [], False),
        )

    def test_plan_with_no_missing_tools_passes_through(self, host_facts):
        plan = make_plan(shell("ls /etc"))
        llm, req = self._patch_llm(plan)
        with llm, req:
            out, messages = adaptive_plan_generation("list etc", host_facts)
        assert out is plan
        assert messages == []

    def test_missing_tool_triggers_regeneration(self, host_facts):
        original = make_plan(shell("frobnicate"))
        regenerated = make_plan(shell("ls /etc"))
        llm, req = self._patch_llm(original)
        with llm, req, patch(
            "src.planning.adaptive_planner.regenerate_plan_with_alternatives",
            return_value=regenerated,
        ):
            out, messages = adaptive_plan_generation("do a thing", host_facts)
        assert out is regenerated
        assert any("Missing tools detected" in m for m in messages)
        assert any("Regenerated plan" in m for m in messages)

    def test_falls_back_to_substitution_when_regeneration_fails(self, sysv_host_facts):
        original = make_plan(shell("systemctl restart ssh"))
        llm, req = self._patch_llm(original)
        with llm, req, patch(
            "src.planning.adaptive_planner.regenerate_plan_with_alternatives", return_value=None
        ):
            out, messages = adaptive_plan_generation("restart ssh", sysv_host_facts)
        assert out.actions[0].argv == ["service", "restart", "ssh"]
        assert any("Using 'service' instead of 'systemctl'" in m for m in messages)

    def test_install_actions_are_prepended(self, host_facts):
        original = make_plan(shell("curl https://example.com"))
        regenerated = make_plan(shell("curl https://example.com"))
        llm, req = self._patch_llm(original)
        with llm, req, patch(
            "src.planning.adaptive_planner.regenerate_plan_with_alternatives",
            return_value=regenerated,
        ):
            out, messages = adaptive_plan_generation(
                "fetch a page", host_facts, auto_install=True
            )
        assert out.actions[0].argv == ["apt", "update"]
        assert any("Will install missing tools" in m for m in messages)

    def test_auto_install_disabled_skips_install_actions(self, host_facts):
        original = make_plan(shell("curl https://example.com"))
        llm, req = self._patch_llm(original)
        with llm, req, patch(
            "src.planning.adaptive_planner.regenerate_plan_with_alternatives",
            return_value=make_plan(shell("ls")),
        ):
            out, messages = adaptive_plan_generation(
                "fetch a page", host_facts, auto_install=False
            )
        assert not any(a.argv[0] == "apt" for a in out.actions)

    def test_llm_failure_raises_planner_error(self, host_facts):
        with patch(
            "src.planning.adaptive_planner.create_plan_generation_request",
            return_value=([], [], False),
        ), patch(
            "src.planning.adaptive_planner.call_llm_with_lookups", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(PlannerError, match="Failed to generate initial plan"):
                adaptive_plan_generation("do a thing", host_facts)
