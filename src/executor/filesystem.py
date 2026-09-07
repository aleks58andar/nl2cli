"""Filesystem action executor for file editing operations."""

import configparser
import re
import shutil
from datetime import datetime
from pathlib import Path

from src.schema import EditFileAction, EditMode
from src.utils import ensure_backup_dir
from src.executor.base import ActionRunner, Result


class EditFileRunner(ActionRunner):
    """Executes file editing actions safely and reliably."""
    
    def __init__(self, action: EditFileAction, sudo_manager=None):
        self.action = action
        self.path = Path(action.path)
        self.sudo_manager = sudo_manager
    
    @property
    def description(self) -> str:
        """Get description of the edit operation."""
        return self.action.description
    
    @property
    def requires_sudo(self) -> bool:
        """Check if this action requires sudo."""
        return self.action.requires_sudo
    
    def run(self, timeout: int = 30) -> Result:
        """Execute the file edit operation."""
        try:
            is_sysfs = str(self.path).startswith(("/sys/", "/proc/"))

            # ── sysfs / procfs: write the raw value directly ──
            if is_sysfs:
                value = self.action.after or self.action.ensure_value or ""
                if not value:
                    return Result(ok=False, stderr="No value to write to sysfs", error_code=1)
                return self._write_sysfs(value)

            # ── regular files ──
            file_exists = self.path.exists()

            if self.action.backup and file_exists:
                backup_result = self._create_backup()
                if not backup_result.ok:
                    return backup_result

            original_content = ""
            if file_exists:
                original_content = self._read_file_content()
                if original_content is None:
                    return Result(ok=False, stderr=f"Could not read {self.path}", error_code=1)

            new_content, changed = self._apply_edit(original_content)

            if not file_exists and not changed and new_content:
                changed = True

            if not (changed or not file_exists):
                return Result(ok=True, stdout=f"No changes needed for {self.path}", changed=False)

            try:
                if self.action.requires_sudo and self.sudo_manager:
                    result = self._write_file_with_sudo(new_content)
                    if result.ok:
                        verb = "Created" if not file_exists else "Edited"
                        return Result(ok=True, stdout=f"{verb} {self.path}", changed=True)
                    return result
                else:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(new_content, encoding='utf-8')
                    verb = "Created" if not file_exists else "Edited"
                    return Result(ok=True, stdout=f"{verb} {self.path}", changed=True)
            except Exception as e:
                return Result(ok=False, stderr=f"Could not write to {self.path}: {e}", error_code=1)

        except Exception as e:
            return Result(ok=False, stderr=f"Unexpected error editing {self.path}: {e}", error_code=1)
    
    def _read_file_content(self) -> str | None:
        """Read file content, falling back to sudo cat if permission denied."""
        import subprocess

        # Try direct read first
        try:
            return self.path.read_text(encoding='utf-8')
        except PermissionError:
            pass
        except UnicodeDecodeError:
            try:
                return self.path.read_text(encoding='latin-1')
            except Exception:
                return None

        # Fall back to sudo cat
        if self.sudo_manager:
            try:
                proc = self.sudo_manager.run_sudo_command(
                    ["cat", str(self.path)], timeout=10,
                )
                if proc.returncode == 0:
                    return proc.stdout
            except Exception:
                pass

        return None

    def _create_backup(self) -> Result:
        """Create a backup of the file, using sudo if needed."""
        try:
            if not self.path.exists():
                return Result(ok=True, stdout="No file to backup")

            backup_dir = ensure_backup_dir()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = str(self.path).replace("/", "_").replace("\\", "_")
            backup_name = f"{safe_name}.{timestamp}.bak"
            backup_path = backup_dir / backup_name

            try:
                shutil.copy2(self.path, backup_path)
            except PermissionError:
                if self.sudo_manager:
                    proc = self.sudo_manager.run_sudo_command(
                        ["cp", "-p", str(self.path), str(backup_path)], timeout=10
                    )
                    if proc.returncode != 0:
                        return Result(
                            ok=False,
                            stderr=f"Backup with sudo failed: {proc.stderr}",
                            error_code=proc.returncode,
                        )
                else:
                    raise

            return Result(
                ok=True,
                stdout=f"Backup created: {backup_path}",
                changed=False,
            )

        except Exception as e:
            return Result(
                ok=False,
                stderr=f"Could not create backup: {e}",
                error_code=1,
            )
    
    def _apply_edit(self, content: str) -> tuple[str, bool]:
        """Apply the edit operation to the content."""
        if self.action.mode == EditMode.replace_line:
            return self._replace_line(content)
        elif self.action.mode == EditMode.insert:
            return self._insert_content(content)
        elif self.action.mode == EditMode.ensure_kv:
            return self._ensure_key_value(content)
        else:
            raise ValueError(f"Unsupported edit mode: {self.action.mode}")
    
    def _replace_line(self, content: str) -> tuple[str, bool]:
        """Replace lines matching the selector with new content."""
        if not self.action.selector or not self.action.after:
            return content, False
        
        lines = content.splitlines()
        new_lines = []
        changed = False
        
        for line in lines:
            if re.search(self.action.selector, line):
                new_lines.append(self.action.after)
                changed = True
            else:
                new_lines.append(line)
        
        return "\n".join(new_lines) + ("\n" if content.endswith("\n") else ""), changed
    
    def _insert_content(self, content: str) -> tuple[str, bool]:
        """Insert content at a specific location."""
        if not self.action.after:
            return content, False
        
        lines = content.splitlines()
        
        if self.action.selector:
            # Find the line to insert after
            for i, line in enumerate(lines):
                if re.search(self.action.selector, line):
                    lines.insert(i + 1, self.action.after)
                    break
            else:
                # Selector not found, append at end
                lines.append(self.action.after)
        else:
            # No selector, append at end
            lines.append(self.action.after)
        
        return "\n".join(lines) + ("\n" if content.endswith("\n") else ""), True
    
    def _ensure_key_value(self, content: str) -> tuple[str, bool]:
        """Ensure a key-value pair exists in config-style files."""
        if not self.action.ensure_key:
            return content, False
        
        key = self.action.ensure_key
        value = self.action.ensure_value or ""
        
        # Try to detect file format and handle accordingly
        if self._looks_like_ini(content):
            return self._ensure_kv_ini(content, key, value)
        else:
            return self._ensure_kv_simple(content, key, value)
    
    def _looks_like_ini(self, content: str) -> bool:
        """Check if content looks like INI format."""
        return bool(re.search(r'^\s*\[.*\]\s*$', content, re.MULTILINE))
    
    def _ensure_kv_ini(self, content: str, key: str, value: str) -> tuple[str, bool]:
        """Handle INI-style configuration files."""
        try:
            # Use configparser for robust INI handling
            config = configparser.ConfigParser()
            config.optionxform = str  # Preserve case
            
            # Parse existing content
            config.read_string(content)
            
            # Determine section (default to DEFAULT or first section)
            section_name = "DEFAULT"
            if config.sections():
                section_name = config.sections()[0]
            
            # Check if key already exists with correct value
            if config.has_option(section_name, key):
                current_value = config.get(section_name, key)
                if current_value == value:
                    return content, False  # No change needed
            
            # Set the value
            if not config.has_section(section_name) and section_name != "DEFAULT":
                config.add_section(section_name)
            
            config.set(section_name, key, value)
            
            # Write back to string
            from io import StringIO
            output = StringIO()
            config.write(output)
            new_content = output.getvalue()
            
            return new_content, True
            
        except Exception:
            # Fall back to simple key-value handling
            return self._ensure_kv_simple(content, key, value)
    
    def _ensure_kv_simple(self, content: str, key: str, value: str) -> tuple[str, bool]:
        """Handle simple key-value files (key=value or key value)."""
        lines = content.splitlines()
        new_lines = []
        key_found = False
        changed = False
        
        # Common key-value patterns
        patterns = [
            rf'^\s*#?\s*{re.escape(key)}\s*=\s*.*$',  # key=value (possibly commented)
            rf'^\s*#?\s*{re.escape(key)}\s+.*$',      # key value (space separated)
        ]
        
        for line in lines:
            line_modified = False
            
            for pattern in patterns:
                if re.match(pattern, line, re.IGNORECASE):
                    # Replace this line with the new key-value
                    if "=" in line or "=" in f"{key} {value}":
                        new_line = f"{key} = {value}"
                    else:
                        new_line = f"{key} {value}"
                    
                    new_lines.append(new_line)
                    key_found = True
                    changed = True
                    line_modified = True
                    break
            
            if not line_modified:
                new_lines.append(line)
        
        # If key wasn't found, add it at the end
        if not key_found:
            if "=" in f"{key} {value}" or not value:
                new_lines.append(f"{key} = {value}")
            else:
                new_lines.append(f"{key} {value}")
            changed = True
        
        return "\n".join(new_lines) + ("\n" if content.endswith("\n") else ""), changed
    
    def _write_sysfs(self, value: str) -> Result:
        """Write a value to a sysfs/procfs virtual file.

        Uses `tee` via sudo (sysfs files can't be opened with O_TRUNC by cp).
        The value is passed on stdin to avoid shell operators.
        """
        value = value.strip()
        try:
            if self.action.requires_sudo and self.sudo_manager:
                proc = self.sudo_manager.run_sudo_command(
                    ["tee", str(self.path)],
                    timeout=10,
                    input=value,
                )
                if proc.returncode == 0:
                    return Result(ok=True, stdout=f"Set {self.path} = {value!r}", changed=True)
                return Result(ok=False, stderr=proc.stderr or "tee failed", error_code=proc.returncode)
            else:
                self.path.write_text(value + "\n")
                return Result(ok=True, stdout=f"Set {self.path} = {value!r}", changed=True)
        except Exception as e:
            return Result(ok=False, stderr=str(e), error_code=1)

    def _write_file_with_sudo(self, content: str) -> Result:
        """Write file content using sudo, creating parent dirs as needed."""
        import tempfile

        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as tmp_file:
                tmp_file.write(content)
                tmp_path = tmp_file.name

            # Ensure the parent directory exists (sudo mkdir -p)
            parent = str(self.path.parent)
            if not self.path.parent.exists():
                mkdir_proc = self.sudo_manager.run_sudo_command(
                    ["mkdir", "-p", parent], timeout=30
                )
                if mkdir_proc.returncode != 0:
                    Path(tmp_path).unlink(missing_ok=True)
                    return Result(
                        ok=False,
                        stderr=f"Failed to create directory {parent}: {mkdir_proc.stderr}",
                        error_code=mkdir_proc.returncode
                    )

            proc = self.sudo_manager.run_sudo_command(
                ["cp", tmp_path, str(self.path)], timeout=30
            )

            Path(tmp_path).unlink(missing_ok=True)

            if proc.returncode == 0:
                return Result(ok=True, stdout=f"File written successfully with sudo: {self.path}")
            else:
                return Result(
                    ok=False,
                    stderr=f"Failed to write file with sudo: {proc.stderr}",
                    error_code=proc.returncode
                )

        except Exception as e:
            return Result(
                ok=False,
                stderr=f"Failed to write file with sudo: {e}",
                error_code=1
            )
