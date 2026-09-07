"""Configuration management for nl2cli."""

import os
from pathlib import Path

try:
    import tomllib
except ImportError:
    # Python < 3.11 compatibility
    import tomli as tomllib

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """Configuration for the LLM model."""
    
    provider: str = "openai"
    model: str = "gpt-4.1"
    temperature: float = 0.1
    max_tokens: int | None = 2000
    timeout: int = 30


class SafetyConfig(BaseModel):
    """Safety configuration for command execution."""
    
    dangerous_patterns: list[str] = Field(default_factory=list)
    allowed_binaries: list[str] = Field(default_factory=list)
    denied_binaries: list[str] = Field(default_factory=list)
    max_file_size_mb: int = 100
    backup_retention_days: int = 30
    allow_shell_glue: bool = False


class AppConfig(BaseModel):
    """Main application configuration."""
    
    model: ModelConfig = Field(default_factory=ModelConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    api_key: str | None = None
    log_level: str = "INFO"
    dry_run_default: bool = False
    auto_backup: bool = True
    confirm_mode: str = "auto"
    action_timeout: int = 30
    auto_install: bool = False


def load_config() -> AppConfig:
    """Load configuration from environment and config files."""
    config = AppConfig()
    
    # Load from config file if it exists
    config_path = Path.home() / ".config" / "nl2cli" / "config.toml"
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                config_data = tomllib.load(f)
            
            # Update config with file data
            if "model" in config_data:
                config.model = ModelConfig(**config_data["model"])
            if "safety" in config_data:
                config.safety = SafetyConfig(**config_data["safety"])
            
            # Update top-level settings
            for key, value in config_data.items():
                if key not in ["model", "safety"] and hasattr(config, key):
                    setattr(config, key, value)
                    
        except Exception as e:
            # If config file is malformed, use defaults but warn
            print(f"Warning: Could not load config file: {e}")
    
    # Override with environment variables
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        config.api_key = api_key
    
    # Model overrides
    if model := os.getenv("NL2CLI_MODEL"):
        config.model.model = model
    
    if temp := os.getenv("NL2CLI_TEMPERATURE"):
        try:
            config.model.temperature = float(temp)
        except ValueError:
            pass
    
    # Log level override
    if log_level := os.getenv("NL2CLI_LOG_LEVEL"):
        config.log_level = log_level.upper()
    
    return config


def get_default_safety_config() -> SafetyConfig:
    """Get default safety configuration with built-in patterns."""
    return SafetyConfig(
        dangerous_patterns=[
            r"rm\s+-rf\s+/\b",
            r"mkfs(\.| )",
            r":\(\)\s*\{\s*:\|\:&\s*\};:",
            r"dd\s+if=.*of=/dev/[hs]d",
            r"fdisk\s+/dev/",
            r"parted\s+/dev/",
            r"format\s+[A-Z]:",
            r"del\s+/[qsf]",
            r"deltree",
            r"shutdown\s+(-h|-r|now)",
            r"reboot\s+(now|-f)",
            r"halt\s+(-f|now)",
            r"init\s+[06]",
            r"kill\s+-9\s+1\b",
            r"pkill\s+-9.*systemd",
            r"rm.*\s+/etc/passwd",
            r"rm.*\s+/etc/shadow",
            r"rm.*\s+/boot/",
            r"grub-install",
            r"update-grub",
            r"lilo\s+-[A-Za-z]*[bB]",
        ],
        allowed_binaries=[
            "sed", "awk", "grep", "cp", "mv", "mkdir", "chmod", "chown",
            "systemctl", "service", "bluetoothd", "hciconfig", "rfkill",
            "apt", "yum", "dnf", "pacman", "zypper",
            "docker", "podman", "systemd-analyze", "journalctl",
            "networkctl", "nmcli", "ip", "ss",
            "cat", "head", "tail", "less", "more",
            "find", "locate", "which", "whereis",
            "ps", "top", "htop", "kill", "killall",
            "mount", "umount", "df", "du", "lsblk",
            "tar", "gzip", "gunzip", "zip", "unzip",
            # Power management
            "system76-power", "powerprofilesctl", "tuned-adm", "tlp",
            "cpupower", "cpufreq-set", "upower", "acpi",
        ],
        denied_binaries=[
            "rm", "rmdir", "shred", "wipe",
            "fdisk", "parted", "mkfs", "fsck",
            "dd", "sync", "sysctl",
            "iptables", "ip6tables", "ufw", "firewall-cmd",
            "setenforce", "getenforce", "setsebool",
            "passwd", "usermod", "userdel", "groupdel",
            "visudo", "sudo", "su",
            "crontab", "at", "batch",
            "nohup", "disown",
        ],
        max_file_size_mb=100,
        backup_retention_days=30
    )


def create_default_config_file() -> Path:
    """Create a default configuration file."""
    config_dir = Path.home() / ".config" / "nl2cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    
    if not config_path.exists():
        default_config = """# nl2cli configuration file

[model]
provider = "openai"
model = "gpt-4"
temperature = 0.1
max_tokens = 2000
timeout = 30

[safety]
max_file_size_mb = 100
backup_retention_days = 30

# Global settings
log_level = "INFO"
dry_run_default = false
auto_backup = true
"""
        config_path.write_text(default_config)
    
    return config_path


# Global config instance
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
