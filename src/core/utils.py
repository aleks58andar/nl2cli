"""Utility functions for system detection and helper operations."""

import platform
import shutil
import subprocess
import sys
from pathlib import Path

from src.core.schema import HostFacts, InitSystem


def run_command(
    argv: list[str],
    capture_output: bool = True,
    text: bool = True,
    check: bool = False,
    timeout: int | None = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a command as an argv list (never uses shell=True)."""
    return subprocess.run(
        argv,
        capture_output=capture_output,
        text=text,
        check=check,
        timeout=timeout,
    )


def which(binary: str) -> str | None:
    """Find the path to a binary, return None if not found."""
    return shutil.which(binary)


def detect_init_system() -> InitSystem:
    """Detect the init system in use."""
    # Check for systemd first (most common on modern systems)
    if which("systemctl") and Path("/run/systemd/system").exists():
        return InitSystem.systemd
    
    # Check for upstart
    if which("initctl") and Path("/etc/init").exists():
        return InitSystem.upstart
    
    # Check for SysV
    if Path("/etc/init.d").exists() and which("service"):
        return InitSystem.sysv
    
    return InitSystem.unknown


def detect_distro() -> tuple[str, str]:
    """Detect Linux distribution ID and version."""
    try:
        # Try to read /etc/os-release (standard on modern systems)
        os_release_path = Path("/etc/os-release")
        if os_release_path.exists():
            os_release = {}
            with open(os_release_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, value = line.split("=", 1)
                        # Remove quotes if present
                        value = value.strip('"\'')
                        os_release[key] = value
            
            distro_id = os_release.get("ID", "unknown")
            version = os_release.get("VERSION_ID", "unknown")
            return distro_id, version
    except Exception:
        pass
    
    # Fallback to platform module
    try:
        distro_info = platform.freedesktop_os_release()
        return distro_info.get("ID", "unknown"), distro_info.get("VERSION_ID", "unknown")
    except Exception:
        pass
    
    # Last resort - use platform.system()
    return platform.system().lower(), "unknown"


def check_binary_availability(binaries: list[str]) -> list[str]:
    """Check which binaries are available on the system."""
    # Shell built-ins and common utilities that are always available
    shell_builtins = {"echo", "cat", "ls", "pwd", "cd", "test", "true", "false", "printf", "touch", "mkdir", "rm", "mv", "cp"}
    
    available = []
    for binary in binaries:
        if binary in shell_builtins or which(binary):
            available.append(binary)
    return available


def check_file_existence(file_paths: list[str]) -> list[str]:
    """Check which files exist on the system."""
    existing = []
    for file_path in file_paths:
        if Path(file_path).exists():
            existing.append(file_path)
    return existing


def detect_machine_identity() -> tuple[str | None, str | None, bool]:
    """Return (vendor, model, is_laptop) from DMI sysfs — no sudo needed."""
    vendor: str | None = None
    model: str | None = None
    is_laptop = False

    try:
        v = Path("/sys/class/dmi/id/sys_vendor").read_text().strip()
        if v:
            vendor = v
    except Exception:
        pass

    try:
        m = Path("/sys/class/dmi/id/product_name").read_text().strip()
        if m:
            model = m
    except Exception:
        pass

    # A battery present → laptop
    try:
        import glob
        is_laptop = bool(glob.glob("/sys/class/power_supply/BAT*"))
    except Exception:
        pass

    return vendor, model, is_laptop


def detect_active_services() -> list[str]:
    """Return names of active systemd services relevant to system management."""
    relevant_patterns = [
        "bluetooth", "network", "wifi", "wpa_supplicant",
        "power", "tlp", "tuned", "system76",
        "docker", "containerd", "podman",
        "ssh", "sshd", "nginx", "apache", "mysql", "postgresql",
        "ufw", "firewalld",
        "cups", "avahi",
        "fwupd", "packagekit",
    ]
    found: list[str] = []
    try:
        proc = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active",
             "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        for line in proc.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            unit = parts[0].removesuffix(".service")
            if any(pat in unit.lower() for pat in relevant_patterns):
                found.append(unit)
    except Exception:
        pass
    return found


def detect_package_manager() -> str | None:
    """Return the name of the available package manager."""
    for pm in ("apt", "dnf", "yum", "pacman", "zypper", "emerge"):
        if which(pm):
            return pm
    return None



def gather_host_facts() -> HostFacts:
    """Gather comprehensive facts about the host system."""
    distro_id, distro_version = detect_distro()
    init_system = detect_init_system()

    common_binaries = [
        # Init / services
        "systemctl", "service", "journalctl", "systemd-analyze",
        # Bluetooth / networking
        "bluetoothd", "hciconfig", "rfkill",
        "networkctl", "nmcli", "ifconfig", "ip", "ss", "netstat",
        # Package managers
        "apt", "apt-get", "yum", "dnf", "pacman", "zypper", "emerge",
        "snap", "flatpak",
        # Power management
        "system76-power", "powerprofilesctl", "tuned-adm", "tlp",
        "cpupower", "cpufreq-set", "upower", "acpi",
        # File / disk utilities
        "df", "du", "stat", "lsblk", "lsof", "mount", "umount",
        "find", "locate", "ls", "cp", "mv", "mkdir", "rm", "touch",
        "cat", "head", "tail", "sort", "uniq", "wc", "cut", "tr",
        "grep", "sed", "awk", "tar", "gzip", "zip", "unzip",
        # System info
        "uname", "uptime", "free", "top", "htop", "ps", "lscpu",
        "lshw", "dmidecode", "hwinfo", "inxi",
        # Dev / misc
        "chmod", "chown", "echo", "printf", "pwd", "test", "true", "false",
        "curl", "wget", "git", "python3", "pip3",
        "docker", "podman",
    ]

    common_files = [
        "/etc/bluetooth/main.conf",
        "/etc/systemd/system",
        "/etc/init.d",
        "/etc/fstab",
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/ssh/sshd_config",
        "/etc/network/interfaces",
        "/etc/netplan",
        "/proc/sys/net/ipv4/ip_forward",
        "/usr/local/bin", "/usr/bin", "/usr/sbin", "/bin", "/sbin",
        "/etc", "/var", "/opt", "/home",
    ]

    available_binaries = check_binary_availability(common_binaries)
    existing_files = check_file_existence(common_files)

    kernel_version: str | None = None
    try:
        kernel_version = platform.release()
    except Exception:
        pass

    machine_vendor, machine_model, is_laptop = detect_machine_identity()
    active_services = detect_active_services()
    package_manager = detect_package_manager()

    return HostFacts(
        distro_id=distro_id,
        distro_version=distro_version,
        init_system=init_system,
        available_binaries=available_binaries,
        existing_files=existing_files,
        kernel_version=kernel_version,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        machine_vendor=machine_vendor,
        machine_model=machine_model,
        is_laptop=is_laptop,
        active_services=active_services,
        package_manager=package_manager,
    )


def ensure_backup_dir() -> Path:
    """Ensure backup directory exists and return its path."""
    backup_dir = Path.home() / ".local" / "state" / "nl2cli" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def ensure_log_dir() -> Path:
    """Ensure log directory exists and return its path."""
    log_dir = Path.home() / ".local" / "state" / "nl2cli" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def is_running_as_root() -> bool:
    """Check if the current process is running as root."""
    import os
    return os.geteuid() == 0


def format_dict_for_prompt(data: dict) -> str:
    """Format a dictionary for inclusion in LLM prompts."""
    lines = []
    for key, value in data.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}: {', '.join(str(v) for v in value[:10])}")
                if len(value) > 10:
                    lines.append(f"  ... and {len(value) - 10} more")
            else:
                lines.append(f"{key}: (none)")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)
