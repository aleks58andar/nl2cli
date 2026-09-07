"""Sudo authentication and session management."""

import logging
import os
import getpass
import subprocess
import threading

logger = logging.getLogger(__name__)

# How often to refresh the sudo timestamp in the background (seconds).
# sudo typically expires after 15 min; refresh every 10 to stay ahead.
_KEEPALIVE_INTERVAL = 600


class SudoManager:
    """Manages sudo authentication and session."""

    def __init__(self):
        self._authenticated = False
        self._username = os.getenv("USER", "user")
        self._keepalive_timer: threading.Timer | None = None

    def check_sudo_needed(self, actions) -> bool:
        return any(getattr(action, "requires_sudo", False) for action in actions)

    def authenticate_if_needed(self, actions) -> bool:
        """Authenticate upfront if any actions need sudo.

        If already running as root, or if the sudo timestamp is still valid
        (e.g. authenticated at startup), returns True immediately with no prompt.
        """
        if not self.check_sudo_needed(actions):
            return True

        # Running as root — no sudo needed at all
        if os.geteuid() == 0:
            self._authenticated = True
            return True

        # Sudo session still live (authenticated at startup or recently used)
        if self._check_sudo_valid():
            self._authenticated = True
            return True

        # No cached session — try a terminal password prompt
        try:
            password = getpass.getpass(f"[sudo] password for {self._username}: ")
            if not password.strip():
                logger.error("sudo authentication failed: no password")
                return False
            if self._test_sudo_password(password):
                self._authenticated = True
                self._start_keepalive()
                return True
            logger.error("sudo authentication failed: wrong password")
            return False
        except (KeyboardInterrupt, EOFError):
            logger.error("sudo authentication cancelled")
            return False
        except Exception as exc:
            logger.error("sudo authentication error: %s", exc)
            return False

    def authenticate_via_gui(self) -> bool:
        """Authenticate using pkexec/graphical prompt (for daemon/tray mode).

        Returns True if authentication succeeded or isn't needed.
        """
        if os.geteuid() == 0 or self._check_sudo_valid():
            self._authenticated = True
            self._start_keepalive()
            return True

        # Use pkexec to show a graphical auth dialog
        import shutil
        if shutil.which("pkexec"):
            try:
                proc = subprocess.run(
                    ["pkexec", "sudo", "-v"],
                    capture_output=True, timeout=60,
                )
                if proc.returncode == 0 and self._check_sudo_valid():
                    self._authenticated = True
                    self._start_keepalive()
                    return True
            except Exception:
                pass

        # Fallback: zenity password dialog
        if shutil.which("zenity"):
            try:
                proc = subprocess.run(
                    ["zenity", "--password", "--title=nl2cli — sudo authentication"],
                    capture_output=True, text=True, timeout=60,
                )
                if proc.returncode == 0:
                    password = proc.stdout.strip()
                    if password and self._test_sudo_password(password):
                        self._authenticated = True
                        self._start_keepalive()
                        return True
            except Exception:
                pass

        logger.warning("Could not authenticate sudo via GUI — sudo commands may fail")
        return False

    def _start_keepalive(self) -> None:
        """Periodically refresh the sudo timestamp so it doesn't expire."""
        self._stop_keepalive()
        timer = threading.Timer(_KEEPALIVE_INTERVAL, self._keepalive_tick)
        timer.daemon = True
        timer.start()
        self._keepalive_timer = timer

    def _keepalive_tick(self) -> None:
        try:
            subprocess.run(["sudo", "-n", "-v"], capture_output=True, timeout=5)
        except Exception:
            pass
        self._start_keepalive()  # reschedule

    def _stop_keepalive(self) -> None:
        if self._keepalive_timer:
            self._keepalive_timer.cancel()
            self._keepalive_timer = None

    def _check_sudo_valid(self) -> bool:
        """Check if current sudo timestamp is valid."""
        try:
            proc = subprocess.run(
                ["sudo", "-n", "true"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _test_sudo_password(self, password: str) -> bool:
        """Test if the provided password works for sudo."""
        try:
            proc = subprocess.Popen(
                ["sudo", "-S", "true"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _stdout, _stderr = proc.communicate(input=password + "\n", timeout=10)
            return proc.returncode == 0
        except Exception:
            return False
    
    def run_sudo_command(self, argv: list[str], timeout: int = 30, input: str | None = None) -> subprocess.CompletedProcess:
        """
        Run a command with sudo using the authenticated session.
        *argv* must NOT contain 'sudo' — it is prepended here.
        """
        return subprocess.run(
            ["sudo"] + argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
    
    @property
    def is_authenticated(self) -> bool:
        """Check if sudo is currently authenticated."""
        return self._authenticated
