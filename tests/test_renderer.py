"""Tests for plan rendering — text preview, rich preview, and script export."""

import pytest

from src.renderer import create_script_preview, render_preview, render_rich_preview
from src.core.schema import EditMode, InitSystem

from .conftest import edit, make_plan, restart, shell


class TestRenderPreview:
    def test_empty_plan(self, host_facts):
        assert render_preview(make_plan(), host_facts) == "# No actions to execute"

    def test_header_carries_the_request(self, host_facts):
        plan = make_plan(shell("ls /etc"), request="list etc")
        assert "# Plan: list etc" in render_preview(plan, host_facts)

    def test_target_system_line_when_a_distro_hint_is_set(self, host_facts):
        plan = make_plan(
            shell("ls"), distro_hint="pop 22.04", init_system=InitSystem.systemd
        )
        assert "# Target system: pop 22.04 (systemd)" in render_preview(plan, host_facts)

    def test_notes_are_shown(self, host_facts):
        plan = make_plan(shell("ls"), notes="requires a reboot")
        assert "# Notes: requires a reboot" in render_preview(plan, host_facts)

    def test_actions_are_numbered_from_one(self, host_facts):
        plan = make_plan(shell("ls"), shell("pwd"))
        text = render_preview(plan, host_facts)
        assert "# Action 1:" in text
        assert "# Action 2:" in text

    def test_sudo_is_flagged(self, host_facts):
        plan = make_plan(shell("systemctl restart ssh", sudo=True))
        assert "(requires sudo)" in render_preview(plan, host_facts)

    def test_risky_actions_are_flagged(self, host_facts):
        plan = make_plan(shell("ls /etc", risky=True))
        assert "RISKY" in render_preview(plan, host_facts)

    def test_every_action_type_renders(self, host_facts):
        plan = make_plan(shell("ls /etc"), edit("/etc/hosts"), restart("ssh"))
        text = render_preview(plan, host_facts)
        assert "# Action 3:" in text
        assert "/etc/hosts" in text
        assert "ssh" in text


class TestRenderRichPreview:
    def test_renders_without_error(self, host_facts, capsys):
        plan = make_plan(shell("ls /etc"), edit("/etc/hosts"), restart("ssh"))
        render_rich_preview(plan, host_facts)
        assert capsys.readouterr().out

    def test_empty_plan_renders_without_error(self, host_facts, capsys):
        render_rich_preview(make_plan(), host_facts)
        capsys.readouterr()  # must not raise

    def test_the_request_appears_in_the_output(self, host_facts, capsys):
        render_rich_preview(make_plan(shell("ls"), request="list things"), host_facts)
        assert "list things" in capsys.readouterr().out


class TestCreateScriptPreview:
    def test_starts_with_a_shebang(self, host_facts):
        script = create_script_preview(make_plan(shell("ls")), host_facts)
        assert script.startswith("#!/bin/bash")

    def test_sets_strict_mode(self, host_facts):
        script = create_script_preview(make_plan(shell("ls")), host_facts)
        assert "set -e" in script
        assert "set -u" in script

    def test_shell_commands_appear_verbatim(self, host_facts):
        script = create_script_preview(make_plan(shell("ls /etc")), host_facts)
        assert "ls /etc" in script

    def test_sudo_is_prepended_for_privileged_actions(self, host_facts):
        script = create_script_preview(
            make_plan(shell("systemctl restart ssh", sudo=True)), host_facts
        )
        assert "sudo systemctl restart ssh" in script

    def test_backups_precede_file_edits(self, host_facts):
        script = create_script_preview(make_plan(edit("/etc/hosts")), host_facts)
        assert "cp '/etc/hosts' '/etc/hosts.bak'" in script

    def test_no_backup_line_when_backup_is_disabled(self, host_facts):
        script = create_script_preview(
            make_plan(edit("/etc/hosts", backup=False)), host_facts
        )
        assert ".bak" not in script

    def test_service_restart_uses_systemctl_on_systemd(self, host_facts):
        script = create_script_preview(make_plan(restart("ssh")), host_facts)
        assert "sudo systemctl restart ssh" in script

    def test_service_restart_uses_the_init_script_on_sysv(self, sysv_host_facts):
        script = create_script_preview(make_plan(restart("ssh")), sysv_host_facts)
        assert "sudo /etc/init.d/ssh restart" in script

    def test_service_restart_falls_back_to_service(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.init_system = InitSystem.unknown
        script = create_script_preview(make_plan(restart("ssh")), facts)
        assert "sudo service ssh restart" in script

    def test_notes_are_included_as_comments(self, host_facts):
        script = create_script_preview(
            make_plan(shell("ls"), notes="needs a reboot"), host_facts
        )
        assert "# needs a reboot" in script

    def test_each_action_is_announced(self, host_facts):
        script = create_script_preview(make_plan(shell("ls"), shell("pwd")), host_facts)
        assert script.count("echo 'Executing action") == 2
