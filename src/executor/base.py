"""Base classes for action execution."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Result:
    """Result of an action execution."""
    
    ok: bool
    stdout: str = ""
    stderr: str = ""
    changed: bool = False
    error_code: int | None = None
    duration_ms: int | None = None
    
    @property
    def success(self) -> bool:
        """Alias for ok."""
        return self.ok
    
    def __str__(self) -> str:
        """String representation of the result."""
        if self.ok:
            status = "SUCCESS"
        else:
            status = f"FAILED ({self.error_code})" if self.error_code else "FAILED"
        
        parts = [f"[{status}]"]
        
        if self.stdout:
            parts.append(f"stdout: {self.stdout.strip()}")
        
        if self.stderr:
            parts.append(f"stderr: {self.stderr.strip()}")
        
        if self.changed:
            parts.append("(changes made)")
        
        return " | ".join(parts)


class ActionRunner(ABC):
    """Abstract base class for action runners."""
    
    @abstractmethod
    def run(self, timeout: int = 30) -> Result:
        """Execute the action and return the result."""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Get a human-readable description of what this action does."""
        pass
    
    @property
    def requires_sudo(self) -> bool:
        """Whether this action requires sudo privileges."""
        return False
    
    def dry_run_description(self) -> str:
        """Get a description of what would happen in a dry run."""
        return f"Would execute: {self.description}"


class ExecutionError(Exception):
    """Raised when action execution fails."""
    
    def __init__(self, message: str, result: Result | None = None):
        super().__init__(message)
        self.result = result
