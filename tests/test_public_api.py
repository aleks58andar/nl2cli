"""Guards the public import surface.

This file exists to make package restructuring safe: it asserts that every
name the rest of the codebase (and any external caller) depends on is
importable and still has the shape callers expect. If a module moves, this
fails loudly rather than at runtime on someone's laptop.
"""

import ast
import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULES = [
    # layer packages
    "src.core",
    "src.planning",
    "src.validation",
    "src.execution",
    "src.storage",
    # modules
    "src.cli",
    "src.core.config",
    "src.core.schema",
    "src.core.state_diff",
    "src.core.utils",
    "src.execution.base",
    "src.execution.filesystem",
    "src.execution.runner",
    "src.execution.services",
    "src.execution.shell",
    "src.execution.sudo_manager",
    "src.planning.adaptive_planner",
    "src.planning.model_client",
    "src.planning.planner",
    "src.renderer",
    "src.storage.audit",
    "src.storage.sysfs_prefs",
    "src.validation.host_compat",
    "src.validation.risk",
    "src.validation.safety",
    "src.validation.validators",
]

# module path -> names that must be importable from it
PUBLIC_NAMES = {
    "src.core.schema": [
        "ActionType", "EditFileAction", "EditMode", "HostFacts", "InitSystem",
        "Plan", "RestartServiceAction", "ShellAction", "ValidationIssue",
    ],
    "src.planning.planner": ["PlannerError", "make_plan"],
    "src.validation.host_compat": [
        "enhance_plan_with_context", "validate_plan_against_host",
    ],
    "src.planning.adaptive_planner": [
        "ToolAlternatives", "adaptive_plan_generation",
        "create_tool_installation_actions", "detect_missing_tools",
        "find_working_alternative", "get_package_manager",
        "regenerate_plan_with_alternatives", "substitute_alternatives_in_plan",
    ],
    "src.planning.model_client": [
        "ModelClientError", "OpenAIClient", "call_llm_structured",
        "call_llm_with_lookups", "create_plan_generation_request",
        "replan_after_failure",
    ],
    "src.validation.validators": [
        "enhance_and_validate_plan", "format_validation_issues",
        "has_critical_issues", "validate_plan",
    ],
    "src.validation.safety": [
        "SafetyChecker", "SafetyError", "get_dangerous_command_reason",
        "get_safety_checker", "is_command_safe",
    ],
    "src.validation.risk": ["compute_risk_score", "needs_confirmation", "one_line_summary"],
    "src.core.config": [
        "AppConfig", "ModelConfig", "SafetyConfig", "get_config",
        "get_default_safety_config", "load_config",
    ],
    "src.storage.audit": ["format_action_result", "write_audit_entry"],
    "src.core.state_diff": ["diff_snapshots", "format_side_effects", "take_snapshot"],
    "src.storage.sysfs_prefs": [
        "quick_restore_side_effects", "restore_desired_values",
        "save_desired_values", "update_preferences_to_new_values",
    ],
    "src.execution.sudo_manager": ["SudoManager"],
    "src.core.utils": ["ensure_backup_dir", "gather_host_facts", "run_command", "which"],
    "src.renderer": ["create_script_preview", "render_preview", "render_rich_preview"],
    "src.cli": ["main"],
    "src.execution.base": ["ActionRunner", "ExecutionError", "Result"],
    "src.execution.shell": [
        "ServiceRunner", "ShellRunner", "SysVServiceRunner", "SystemctlRunner",
    ],
    "src.execution.filesystem": ["EditFileRunner"],
    "src.execution.services": ["UpstartServiceRunner", "get_service_runner"],
    "src.execution.runner": ["get_action_runner", "run_plan"],
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


class TestLayerPackages:
    LAYERS = ["src.core", "src.planning", "src.validation", "src.execution", "src.storage"]

    @pytest.mark.parametrize("package", LAYERS)
    def test_declared_exports_all_resolve(self, package):
        module = importlib.import_module(package)
        assert module.__all__, f"{package} declares no public surface"
        for name in module.__all__:
            assert hasattr(module, name), f"{package}.{name} is missing"

    @pytest.mark.parametrize("package", LAYERS)
    def test_every_layer_documents_itself(self, package):
        assert importlib.import_module(package).__doc__

    def test_execution_exports_the_runner_entry_points(self):
        import src.execution as execution

        assert {"run_plan", "get_action_runner", "Result"} <= set(execution.__all__)


class TestLayering:
    """The dependency direction is part of the design, so it is tested.

    core is the bottom: it imports from no other layer. planning, validation,
    execution and storage may use core but not each other. cli and renderer sit
    on top and may use anything.
    """

    ALLOWED = {
        "core": set(),
        "planning": {"core"},
        "validation": {"core"},
        "execution": {"core"},
        "storage": {"core"},
    }

    @staticmethod
    def _imported_layers(path: Path) -> set[str]:
        tree = ast.parse(path.read_text())
        found = set()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                parts = name.split(".")
                if len(parts) >= 2 and parts[0] == "src":
                    found.add(parts[1])
        return found

    @pytest.mark.parametrize("layer", sorted(ALLOWED))
    def test_layer_only_imports_what_it_may(self, layer):
        package_dir = Path(__file__).resolve().parent.parent / "src" / layer
        for module_file in sorted(package_dir.glob("*.py")):
            imported = self._imported_layers(module_file) - {layer}
            illegal = imported - self.ALLOWED[layer]
            assert not illegal, (
                f"src/{layer}/{module_file.name} imports from {sorted(illegal)}, "
                f"which {layer} may not depend on"
            )


class TestCallableSignatures:
    """Argument names are part of the contract — callers use keywords."""

    @pytest.mark.parametrize(
        "module_path,name,expected",
        [
            ("src.planning.planner", "make_plan", ["nl_request", "host_facts"]),
            (
                "src.validation.host_compat",
                "validate_plan_against_host",
                ["plan", "host_facts"],
            ),
            ("src.validation.validators", "validate_plan", ["plan", "host_facts"]),
            ("src.validation.risk", "compute_risk_score", ["plan"]),
            ("src.validation.risk", "needs_confirmation", ["plan", "confirm_mode"]),
            ("src.core.state_diff", "diff_snapshots", ["before", "after"]),
            (
                "src.execution.runner",
                "run_plan",
                ["plan", "host_facts", "verbose", "dry_run", "action_timeout"],
            ),
            (
                "src.planning.adaptive_planner",
                "adaptive_plan_generation",
                ["user_request", "host_facts", "auto_install"],
            ),
        ],
    )
    def test_signature_is_stable(self, module_path, name, expected):
        func = getattr(importlib.import_module(module_path), name)
        assert list(inspect.signature(func).parameters) == expected

    def test_audit_entry_is_keyword_only(self):
        from src.storage.audit import write_audit_entry

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
