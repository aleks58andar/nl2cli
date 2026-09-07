"""Tests for sudo authentication and command routing.

No test may prompt for or transmit a real password: every subprocess call
and every getpass call is mocked.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.execution.sudo_manager import SudoManager

from .conftest import edit, shell


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def manager():
    return SudoManager()


class TestCheckSudoNeeded:
    def test_no_actions_need_no_sudo(self, manager):
        assert manager.check_sudo_needed([]) is False

    def test_plain_actions_need_no_sudo(self, manager):
        assert manager.check_sudo_needed([shell("ls"), edit("/tmp/f")]) is False

    def test_one_sudo_action_is_enough(self, manager):
        assert manager.check_sudo_needed([shell("ls"), shell("ls /root", sudo=True)])

    def test_objects_without_the_attribute_are_tolerated(self, manager):
        assert manager.check_sudo_needed([object()]) is False


class TestAuthenticateIfNeeded:
    def test_returns_immediately_when_no_action_needs_sudo(self, manager):
        with patch("subprocess.run") as run, patch("getpass.getpass") as getpass:
            assert manager.authenticate_if_needed([shell("ls")]) is True
        run.assert_not_called()
        getpass.assert_not_called()

    def test_root_never_prompts(self, manager):
        with patch("os.geteuid", return_value=0), patch("getpass.getpass") as getpass:
            assert manager.authenticate_if_needed([shell("ls", sudo=True)]) is True
        getpass.assert_not_called()
        assert manager.is_authenticated is True

    def test_a_live_sudo_timestamp_skips_the_prompt(self, manager):
        with patch("os.geteuid", return_value=1000), patch(
            "subprocess.run", return_value=completed(0)
        ), patch("getpass.getpass") as getpass:
            assert manager.authenticate_if_needed([shell("ls", sudo=True)]) is True
        getpass.assert_not_called()

    def test_correct_password_authenticates_and_starts_keepalive(self, manager):
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0

        with patch("os.geteuid", return_value=1000), patch(
            "subprocess.run", return_value=completed(1)
        ), patch("getpass.getpass", return_value="hunter2"), patch(
            "subprocess.Popen", return_value=proc
        ), patch.object(manager, "_start_keepalive") as keepalive:
            assert manager.authenticate_if_needed([shell("ls", sudo=True)]) is True

        assert manager.is_authenticated is True
        keepalive.assert_called_once()

    def test_wrong_password_fails(self, manager):
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 1

        with patch("os.geteuid", return_value=1000), patch(
            "subprocess.run", return_value=completed(1)
        ), patch("getpass.getpass", return_value="wrong"), patch(
            "subprocess.Popen", return_value=proc
        ):
            assert manager.authenticate_if_needed([shell("ls", sudo=True)]) is False
        assert manager.is_authenticated is False

    def test_empty_password_fails_without_invoking_sudo(self, manager):
        with patch("os.geteuid", return_value=1000), patch(
            "subprocess.run", return_value=completed(1)
        ), patch("getpass.getpass", return_value="   "), patch(
            "subprocess.Popen"
        ) as popen:
            assert manager.authenticate_if_needed([shell("ls", sudo=True)]) is False
        popen.assert_not_called()

    def test_cancelled_prompt_fails_cleanly(self, manager):
        with patch("os.geteuid", return_value=1000), patch(
            "subprocess.run", return_value=completed(1)
        ), patch("getpass.getpass", side_effect=KeyboardInterrupt):
            assert manager.authenticate_if_needed([shell("ls", sudo=True)]) is False

    def test_eof_on_the_prompt_fails_cleanly(self, manager):
        with patch("os.geteuid", return_value=1000), patch(
            "subprocess.run", return_value=completed(1)
        ), patch("getpass.getpass", side_effect=EOFError):
            assert manager.authenticate_if_needed([shell("ls", sudo=True)]) is False


class TestRunSudoCommand:
    def test_sudo_is_prepended_to_the_argv(self, manager):
        with patch("subprocess.run", return_value=completed()) as run:
            manager.run_sudo_command(["systemctl", "restart", "ssh"])
        assert run.call_args[0][0] == ["sudo", "systemctl", "restart", "ssh"]

    def test_output_is_captured_as_text(self, manager):
        with patch("subprocess.run", return_value=completed()) as run:
            manager.run_sudo_command(["ls"])
        assert run.call_args.kwargs["capture_output"] is True
        assert run.call_args.kwargs["text"] is True

    def test_timeout_is_forwarded(self, manager):
        with patch("subprocess.run", return_value=completed()) as run:
            manager.run_sudo_command(["ls"], timeout=7)
        assert run.call_args.kwargs["timeout"] == 7

    def test_stdin_input_is_forwarded(self, manager):
        with patch("subprocess.run", return_value=completed()) as run:
            manager.run_sudo_command(["tee", "/sys/x"], input="2")
        assert run.call_args.kwargs["input"] == "2"

    def test_the_completed_process_is_returned(self, manager):
        expected = completed(returncode=3, stdout="out")
        with patch("subprocess.run", return_value=expected):
            assert manager.run_sudo_command(["ls"]) is expected


class TestSudoValidity:
    def test_zero_exit_means_a_valid_timestamp(self, manager):
        with patch("subprocess.run", return_value=completed(0)) as run:
            assert manager._check_sudo_valid() is True
        assert run.call_args[0][0] == ["sudo", "-n", "true"]

    def test_nonzero_exit_means_no_valid_timestamp(self, manager):
        with patch("subprocess.run", return_value=completed(1)):
            assert manager._check_sudo_valid() is False

    def test_subprocess_failure_is_treated_as_invalid(self, manager):
        with patch("subprocess.run", side_effect=OSError("nope")):
            assert manager._check_sudo_valid() is False

    def test_timeout_is_treated_as_invalid(self, manager):
        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="sudo", timeout=5)
        ):
            assert manager._check_sudo_valid() is False


class TestKeepalive:
    def test_starting_schedules_a_daemon_timer(self, manager):
        manager._start_keepalive()
        try:
            assert manager._keepalive_timer is not None
            assert manager._keepalive_timer.daemon is True
        finally:
            manager._stop_keepalive()

    def test_stopping_clears_the_timer(self, manager):
        manager._start_keepalive()
        manager._stop_keepalive()
        assert manager._keepalive_timer is None

    def test_starting_twice_does_not_leak_the_first_timer(self, manager):
        manager._start_keepalive()
        first = manager._keepalive_timer
        manager._start_keepalive()
        try:
            assert manager._keepalive_timer is not first
            assert first.finished.is_set()
        finally:
            manager._stop_keepalive()

    def test_a_tick_refreshes_the_timestamp_and_reschedules(self, manager):
        with patch("subprocess.run", return_value=completed()) as run, patch.object(
            manager, "_start_keepalive"
        ) as restart:
            manager._keepalive_tick()
        assert run.call_args[0][0] == ["sudo", "-n", "-v"]
        restart.assert_called_once()

    def test_a_failing_tick_still_reschedules(self, manager):
        with patch("subprocess.run", side_effect=OSError), patch.object(
            manager, "_start_keepalive"
        ) as restart:
            manager._keepalive_tick()
        restart.assert_called_once()


class TestGuiAuthentication:
    def test_root_short_circuits(self, manager):
        with patch("os.geteuid", return_value=0), patch.object(manager, "_start_keepalive"):
            assert manager.authenticate_via_gui() is True

    def test_pkexec_success_authenticates(self, manager):
        with patch("os.geteuid", return_value=1000), patch(
            "shutil.which", side_effect=lambda b: "/usr/bin/pkexec" if b == "pkexec" else None
        ), patch("subprocess.run", return_value=completed(0)), patch.object(
            manager, "_start_keepalive"
        ):
            assert manager.authenticate_via_gui() is True
        assert manager.is_authenticated is True

    def test_no_gui_helpers_available_fails(self, manager):
        with patch("os.geteuid", return_value=1000), patch(
            "shutil.which", return_value=None
        ), patch("subprocess.run", return_value=completed(1)):
            assert manager.authenticate_via_gui() is False
