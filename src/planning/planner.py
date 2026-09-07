"""Natural language to structured plan conversion."""


from src.planning.model_client import (
    create_plan_generation_request,
    call_llm_with_lookups,
)
from src.core.schema import Plan, HostFacts


class PlannerError(Exception):
    """Raised when plan generation fails."""


def make_plan(nl_request: str, host_facts: HostFacts) -> Plan:
    """Convert a natural language request into a structured plan.

    Uses the multi-turn loop so the LLM can call lookup tools (get_tool_usage,
    check_path, get_service_info) before emitting the plan.
    """
    try:
        messages, tools, has_prefetch = create_plan_generation_request(nl_request, host_facts)

        plan = call_llm_with_lookups(
            messages=messages,
            tools=tools,
            host_facts=host_facts,
            result_model=Plan,
            has_prefetch=has_prefetch,
        )

        plan.init_system = host_facts.init_system
        plan.distro_hint = f"{host_facts.distro_id} {host_facts.distro_version}"
        return plan

    except Exception as e:
        raise PlannerError(f"Failed to generate plan: {e}") from e
