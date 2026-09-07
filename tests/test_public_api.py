"""Guards the public import surface.

This file exists to make package restructuring safe: it asserts that every
name the rest of the codebase (and any external caller) depends on is
importable and still has the shape callers expect. If a module moves, this
fails loudly rather than at runtime on someone's laptop.
"""

import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULES = [
    "src.adaptive_planner",
    "src.audit",
    "src.cli",
    "src.config",
    "src.executor",
    "src.executor.base",
    "src.executor.filesystem",
    "src.executor.main",
    "src.executor.services",
    "src.executor.shell",
    "src.model_client",
    "src.planner",
    "src.renderer",
    "src.risk",
    "src.safety",
    "src.schema",
    "src.state_diff",
    "src.sudo_manager",
    "src.sysfs_prefs",
    "src.utils",
    "src.validators",
]

# module path -> names that must be importable from it
PUBLIC_NAMES = {
    "src.schema": [
        "ActionType", "EditFileAction", "EditMode", "HostFacts", "InitSystem",
        "Plan", "RestartServiceAction", "ShellAction", "ValidationIssue",
    ],
    "src.planner": [
        "PlannerError", "enhance_plan_with_context", "make_plan",
        "validate_plan_against_host",
    ],
    "src.adaptive_planner": [
        "ToolAlternatives", "adaptive_plan_generation",
        "create_tool_installation_actions", "detect_missing_tools",
        "find_working_alternative", "get_package_manager",
        "regenerate_plan_with_alternatives", "substitute_alternatives_in_plan",
    ],
    "src.model_client": [
        "ModelClientError", "OpenAIClient", "call_llm_structured",
        "call_llm_with_lookups", "create_plan_generation_request",
        "replan_after_failure",
    ],
    "src.validators": [
        "enhance_and_validate_plan", "format_validation_issues",
        "has_critical_issues", "validate_plan",
    ],
    "src.safety": [
        "SafetyChecker", "SafetyError", "get_dangerous_command_reason",
        "get_safety_checker", "is_command_safe",
    ],
    "src.risk": ["compute_risk_score", "needs_confirmation", "one_line_summary"],
    "src.config": [
        "AppConfig", "ModelConfig", "SafetyConfig", "get_config",
        "get_default_safety_config", "load_config",
    ],
    "src.audit": ["format_action_result", "write_audit_entry"],
    "src.state_diff": ["diff_snapshots", "format_side_effects", "take_snapshot"],
    "src.sysfs_prefs": [
        "quick_restore_side_effects", "restore_desired_values",
        "save_desired_values", "update_preferences_to_new_values",
    ],
    "src.sudo_manager": ["SudoManager"],
    "src.utils": ["ensure_backup_dir", "gather_host_facts", "run_command", "which"],
    "src.renderer": ["create_script_preview", "render_preview", "render_rich_preview"],
    "src.cli": ["main"],
    "src.executor.base": ["ActionRunner", "ExecutionError", "Result"],
    "src.executor.shell": [
        "ServiceRunner", "ShellRunner", "SysVServiceRunner", "SystemctlRunner",
    ],
    "src.executor.filesystem": ["EditFileRunner"],
    "src.executor.services": ["UpstartServiceRunner", "get_service_runner"],
    "src.executor.main": ["get_action_runner", "run_plan"],
}


@pytest.mark.parametrize("module_path", MODULES)
def test_module_imports(module_path):
    assert importlib.import_module(module_path) is not None


@pytest.mark.parametrize(
    "module_path,name",
    [(m, n) for m, names in PUBLIC_NAMES.items() for n in names],
)
def test_public_name_is_available(module_path, name):
    module = importlib.import_module(module_path)
    assert hasattr(module, name), f"{module_path}.{name} is missing"


class TestExecutorPackageExports:
    def test_declared_exports_all_resolve(self):
        import src.executor as executor

        for name in executor.__all__:
            assert hasattr(executor, name), f"src.executor.{name} is missing"

    def test_the_export_list_is_the_documented_one(self):
        import src.executor as executor

        assert set(executor.__all__) == {
            "ActionRunner",
            "Result",
            "EditFileRunner",
            "ShellRunner",
            "get_service_runner",
            "run_plan",
        }


class TestCallableSignatures:
    """Argument names are part of the contract — callers use keywords."""

    @pytest.mark.parametrize(
        "module_path,name,expected",
        [
            ("src.planner", "make_plan", ["nl_request", "host_facts"]),
            ("src.validators", "validate_plan", ["plan", "host_facts"]),
            ("src.risk", "compute_risk_score", ["plan"]),
            ("src.risk", "needs_confirmation", ["plan", "confirm_mode"]),
            ("src.state_diff", "diff_snapshots", ["before", "after"]),
            (
                "src.executor.main",
                "run_plan",
                ["plan", "host_facts", "verbose", "dry_run", "action_timeout"],
            ),
            (
                "src.adaptive_planner",
                "adaptive_plan_generation",
                ["user_request", "host_facts", "auto_install"],
            ),
        ],
    )
    def test_signature_is_stable(self, module_path, name, expected):
        func = getattr(importlib.import_module(module_path), name)
        assert list(inspect.signature(func).parameters) == expected

    def test_audit_entry_is_keyword_only(self):
        from src.audit import write_audit_entry

        params = inspect.signature(write_audit_entry).parameters
        assert all(p.kind is p.KEYWORD_ONLY for p in params.values())
        assert set(params) == {
            "transcript",
            "plan",
            "validator_findings",
            "confirmation_required",
            "confirmation_granted",
            "action_results",
        }


class TestNoImportSideEffects:
    def test_importing_never_requires_an_api_key(self):
        """A missing key must not break imports — only client construction.

        Runs in a subprocess: reloading these modules in-process would rebuild
        the pydantic classes and break isinstance checks in every other test.
        """
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        script = "import importlib\n" + "\n".join(
            f"importlib.import_module({m!r})" for m in MODULES
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env
        )
        assert proc.returncode == 0, proc.stderr

    def test_the_console_script_entry_point_resolves(self):
        from src.cli import main

        assert callable(main)
