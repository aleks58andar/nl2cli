"""Tests for the LLM boundary.

No test makes a network call: the OpenAI client is replaced by a fake that
returns canned tool calls. What is under test is our side of the boundary —
prompt assembly, the lookup loop, pre-fetch, and error translation.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.planning.model_client import (
    MAX_FALLBACK_ROUNDS,
    ModelClientError,
    OpenAIClient,
    _build_host_context,
    _execute_lookup,
    _lookup_check_path,
    _lookup_tool_usage,
    _prefetch_context,
    create_plan_generation_request,
    format_dict_for_prompt,
)
from src.core.schema import Plan


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

def tool_call(name: str, arguments: dict, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def response(tool_calls=None, content=None):
    message = SimpleNamespace(tool_calls=tool_calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


PLAN_ARGS = {
    "user_request": "list etc",
    "actions": [{"type": "shell", "description": "list", "command": "ls /etc"}],
}


@pytest.fixture
def client(monkeypatch):
    """An OpenAIClient whose transport is a mock, with a stub API key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch("src.planning.model_client.OpenAI") as openai:
        instance = OpenAIClient(api_key="sk-test")
        instance.client = MagicMock()
        instance.create = instance.client.chat.completions.create
        yield instance


class TestClientConstruction:
    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with patch("src.planning.model_client.get_config") as get_config:
            get_config.return_value.api_key = None
            with pytest.raises(ModelClientError, match="API key not found"):
                OpenAIClient()

    def test_explicit_key_is_used(self):
        with patch("src.planning.model_client.OpenAI") as openai:
            OpenAIClient(api_key="sk-explicit")
        assert openai.call_args.kwargs["api_key"] == "sk-explicit"


class TestForcedPlanCall:
    def test_prefetch_forces_a_single_api_call(self, client, host_facts):
        client.create.return_value = response([tool_call("emit_plan", PLAN_ARGS)])
        plan = client.call_with_lookups([], [], host_facts, Plan, has_prefetch=True)

        assert isinstance(plan, Plan)
        assert client.create.call_count == 1
        assert client.create.call_args.kwargs["tool_choice"] == {
            "type": "function",
            "function": {"name": "emit_plan"},
        }

    def test_empty_choices_raise(self, client, host_facts):
        client.create.return_value = SimpleNamespace(choices=[])
        with pytest.raises(ModelClientError, match="No response choices"):
            client.call_with_lookups([], [], host_facts, Plan, has_prefetch=True)

    def test_missing_tool_call_raises(self, client, host_facts):
        client.create.return_value = response(tool_calls=None, content="sorry")
        with pytest.raises(ModelClientError, match="No tool calls in forced response"):
            client.call_with_lookups([], [], host_facts, Plan, has_prefetch=True)

    def test_malformed_json_raises(self, client, host_facts):
        bad = SimpleNamespace(
            id="1", type="function",
            function=SimpleNamespace(name="emit_plan", arguments="{not json"),
        )
        client.create.return_value = response([bad])
        with pytest.raises(ModelClientError, match="Bad JSON"):
            client.call_with_lookups([], [], host_facts, Plan, has_prefetch=True)

    def test_schema_violation_is_reported_as_validation_failure(
        self, client, host_facts
    ):
        """An unsafe command fails Plan construction — the model cannot smuggle it."""
        injected = {
            "user_request": "x",
            "actions": [
                {"type": "shell", "description": "d", "command": "ls; rm -rf /"}
            ],
        }
        client.create.return_value = response([tool_call("emit_plan", injected)])
        with pytest.raises(ModelClientError, match="Plan validation failed"):
            client.call_with_lookups([], [], host_facts, Plan, has_prefetch=True)


class TestLookupLoop:
    def test_a_plan_on_the_first_round_ends_the_loop(self, client, host_facts):
        client.create.return_value = response([tool_call("emit_plan", PLAN_ARGS)])
        client.call_with_lookups([], [], host_facts, Plan)
        assert client.create.call_count == 1

    def test_lookups_are_resolved_then_the_plan_is_accepted(self, client, host_facts):
        client.create.side_effect = [
            response([tool_call("check_path", {"path": "/etc"})]),
            response([tool_call("emit_plan", PLAN_ARGS)]),
        ]
        messages: list[dict] = []
        plan = client.call_with_lookups(messages, [], host_facts, Plan)

        assert isinstance(plan, Plan)
        assert client.create.call_count == 2
        assert [m["role"] for m in messages] == ["assistant", "tool"]
        assert "Directory exists: /etc" in messages[1]["content"]

    def test_the_loop_is_bounded_then_forces_a_plan(self, client, host_facts):
        lookup = response([tool_call("check_path", {"path": "/etc"})])
        client.create.side_effect = [lookup] * MAX_FALLBACK_ROUNDS + [
            response([tool_call("emit_plan", PLAN_ARGS)])
        ]
        client.call_with_lookups([], [], host_facts, Plan)
        assert client.create.call_count == MAX_FALLBACK_ROUNDS + 1

    def test_a_conversational_reply_is_pushed_back_to_a_plan(self, client, host_facts):
        client.create.side_effect = [
            response(tool_calls=None, content="Sure, here's how you'd do that."),
            response([tool_call("emit_plan", PLAN_ARGS)]),
        ]
        messages: list[dict] = []
        plan = client.call_with_lookups(messages, [], host_facts, Plan)

        assert isinstance(plan, Plan)
        assert any("must always call emit_plan" in m.get("content", "") for m in messages)


class TestExecuteLookup:
    def test_unknown_tool_is_reported_not_raised(self, host_facts):
        assert "Unknown lookup tool" in _execute_lookup("nope", {}, host_facts)

    def test_check_path_dispatch(self, host_facts):
        assert "Directory exists: /etc" in _execute_lookup(
            "check_path", {"path": "/etc"}, host_facts
        )

    def test_missing_arguments_are_handled(self, host_facts):
        assert "path is required" in _execute_lookup("check_path", {}, host_facts)

    def test_tool_usage_refuses_uninstalled_binaries(self, host_facts):
        assert "not installed" in _lookup_tool_usage("frobnicate", None, host_facts)

    def test_tool_usage_requires_a_name(self, host_facts):
        assert "tool_name is required" in _lookup_tool_usage("", None, host_facts)


class TestCheckPath:
    def test_missing_path(self):
        assert _lookup_check_path("/definitely/not/here").startswith("Does not exist")

    def test_empty_path(self):
        assert "path is required" in _lookup_check_path("")

    def test_small_file_is_shown_whole(self, tmp_path):
        f = tmp_path / "conf"
        f.write_text("Port 22\n")
        out = _lookup_check_path(str(f))
        assert "File exists" in out
        assert "Port 22" in out

    def test_large_file_is_truncated(self, tmp_path):
        f = tmp_path / "big"
        f.write_text("\n".join(f"line {i}" for i in range(200)))
        out = _lookup_check_path(str(f))
        assert "200 lines" in out
        assert "line 199" not in out

    def test_directory_listing_is_capped(self, tmp_path):
        for i in range(80):
            (tmp_path / f"f{i:03d}").write_text("")
        out = _lookup_check_path(str(tmp_path))
        assert out.count("[f]") == 50


class TestPrefetch:
    def test_unrelated_request_prefetches_nothing(self, host_facts):
        assert _prefetch_context("write a poem", host_facts) == []

    def test_keyword_triggers_a_sysfs_lookup(self, host_facts):
        messages = _prefetch_context("turn on the keyboard backlight", host_facts)
        assert messages, "expected a pre-fetched lookup"
        assert messages[0]["role"] == "assistant"
        assert messages[1]["role"] == "tool"
        assert messages[0]["tool_calls"][0]["function"]["name"] == "check_sysfs"

    def test_named_binary_triggers_a_help_lookup(self, host_facts):
        facts = host_facts.model_copy(deep=True)
        facts.available_binaries = facts.available_binaries + ["powerprofilesctl"]
        names = [
            m["tool_calls"][0]["function"]["name"]
            for m in _prefetch_context("use powerprofilesctl please", facts)
            if m["role"] == "assistant"
        ]
        assert "get_tool_usage" in names

    def test_common_binaries_are_not_looked_up(self, host_facts):
        assert _prefetch_context("use grep to find things", host_facts) == []

    def test_messages_come_in_assistant_tool_pairs(self, host_facts):
        messages = _prefetch_context("keyboard backlight and screen brightness", host_facts)
        assert len(messages) % 2 == 0
        assert all(m["role"] == "assistant" for m in messages[0::2])
        assert all(m["role"] == "tool" for m in messages[1::2])

    def test_tool_results_are_linked_by_call_id(self, host_facts):
        messages = _prefetch_context("keyboard backlight", host_facts)
        assert messages[0]["tool_calls"][0]["id"] == messages[1]["tool_call_id"]


class TestRequestAssembly:
    def test_emit_plan_tool_carries_the_plan_schema(self, host_facts):
        _, tools, _ = create_plan_generation_request("list etc", host_facts)
        emit = [t for t in tools if t["function"]["name"] == "emit_plan"]
        assert len(emit) == 1
        assert "actions" in emit[0]["function"]["parameters"]["properties"]

    def test_lookup_tools_are_offered_alongside_it(self, host_facts):
        _, tools, _ = create_plan_generation_request("list etc", host_facts)
        names = {t["function"]["name"] for t in tools}
        assert {"get_tool_usage", "check_path", "check_sysfs", "get_service_info"} <= names

    def test_the_request_reaches_the_model_verbatim(self, host_facts):
        messages, _, _ = create_plan_generation_request("enable bluetooth", host_facts)
        assert any(
            m["role"] == "user" and m["content"] == "enable bluetooth" for m in messages
        )

    def test_host_context_is_included(self, host_facts):
        messages, _, _ = create_plan_generation_request("list etc", host_facts)
        system_text = " ".join(m["content"] for m in messages if m["role"] == "system")
        assert "systemd" in system_text
        assert "Available binaries" in system_text

    def test_prefetch_flag_is_false_for_an_unrelated_request(self, host_facts):
        _, _, has_prefetch = create_plan_generation_request("write a poem", host_facts)
        assert has_prefetch is False

    def test_prefetch_flag_is_true_when_context_was_gathered(self, host_facts):
        _, _, has_prefetch = create_plan_generation_request(
            "keyboard backlight on", host_facts
        )
        assert has_prefetch is True


class TestHostContext:
    def test_includes_the_machine_identity(self, host_facts):
        text = _build_host_context(host_facts)
        assert "pop" in text
        assert "System76" in text

    def test_includes_the_package_manager(self, host_facts):
        assert "apt" in _build_host_context(host_facts)


class TestFormatDictForPrompt:
    def test_scalars_are_rendered_as_key_value(self):
        assert format_dict_for_prompt({"distro": "pop"}) == "distro: pop"

    def test_lists_are_joined_and_capped(self):
        out = format_dict_for_prompt({"bins": [str(i) for i in range(15)]})
        assert "and 5 more" in out

    def test_empty_lists_are_marked(self):
        assert format_dict_for_prompt({"bins": []}) == "bins: (none)"

    def test_none_values_are_skipped(self):
        assert format_dict_for_prompt({"a": None, "b": 1}) == "b: 1"

    def test_dicts_are_json_encoded(self):
        assert format_dict_for_prompt({"d": {"k": "v"}}) == 'd: {"k": "v"}'
