"""Tests for plan generation."""

from unittest.mock import patch

import pytest

from src.core.schema import Plan
from src.planning.planner import PlannerError, make_plan

from .conftest import make_plan as build_plan, shell


class TestMakePlan:
    """The LLM call is mocked — only the planner's own wiring is under test."""

    def test_host_metadata_is_stamped_onto_the_plan(self, host_facts):
        returned = Plan(user_request="list etc", actions=[])
        with patch(
            "src.planning.planner.create_plan_generation_request", return_value=([], [], False)
        ), patch("src.planning.planner.call_llm_with_lookups", return_value=returned):
            plan = make_plan("list etc", host_facts)

        assert plan.init_system == "systemd"
        assert plan.distro_hint == "pop 22.04"

    def test_the_prefetch_flag_is_forwarded(self, host_facts):
        returned = Plan(user_request="x", actions=[])
        with patch(
            "src.planning.planner.create_plan_generation_request", return_value=([], [], True)
        ), patch(
            "src.planning.planner.call_llm_with_lookups", return_value=returned
        ) as call:
            make_plan("x", host_facts)
        assert call.call_args.kwargs["has_prefetch"] is True

    def test_llm_errors_are_wrapped(self, host_facts):
        with patch(
            "src.planning.planner.create_plan_generation_request", return_value=([], [], False)
        ), patch(
            "src.planning.planner.call_llm_with_lookups", side_effect=RuntimeError("no network")
        ):
            with pytest.raises(PlannerError, match="Failed to generate plan"):
                make_plan("x", host_facts)

    def test_the_original_error_is_kept_as_the_cause(self, host_facts):
        original = RuntimeError("no network")
        with patch(
            "src.planning.planner.create_plan_generation_request", return_value=([], [], False)
        ), patch("src.planning.planner.call_llm_with_lookups", side_effect=original):
            with pytest.raises(PlannerError) as exc:
                make_plan("x", host_facts)
        assert exc.value.__cause__ is original
