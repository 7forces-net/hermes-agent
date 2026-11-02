"""Thinking-exhaustion truncation escalates to the fallback chain.

A model that burns its ENTIRE output budget on reasoning and produces no visible
text cannot be continued on the SAME leg: the next continuation re-burns the same
budget. Before this change the thinking-exhaustion branch of
``agent/turn_truncation.py`` ended the turn unconditionally, so a worker that
exhausted its thinking budget on the LAST live leg had nowhere to go and the turn
was lost.

The escalation mirrors the content-filter branch: the partial content is rolled back
to the last clean turn, the fallback provider is activated, and the call is re-issued
against the new provider.

Three properties are pinned here:

1. With a live fallback leg the fallback IS activated and the turn continues.
2. With an EMPTY fallback chain the pre-existing terminal behaviour and its message
   text are unchanged.
3. ``_fallback_index`` is never stranded past the end of the chain by the new call
   site (the bug class already fixed in ``agent/agent_runtime_helpers.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import FINISH_REASON_LENGTH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _thinking_only_length_response():
    """finish_reason='length' whose content is ONLY a think block.

    That is the shape ``_abort_reason`` recognises as thinking exhaustion: the
    think tag is present and no visible text follows it.
    """
    from tests.agent.test_run_agent import _mock_assistant_msg

    return SimpleNamespace(
        id="chatcmpl-thinking-exhausted",
        model="test/model",
        choices=[SimpleNamespace(
            index=0,
            message=_mock_assistant_msg(content="<think>burned the whole budget</think>"),
            finish_reason=FINISH_REASON_LENGTH,
        )],
        usage=None,
    )


def _full_response(content):
    from tests.agent.test_run_agent import _mock_response

    return _mock_response(content=content, finish_reason="stop")


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.compression_enabled = False
        a.save_trajectories = False
        return a


def _run(agent, message, history=None):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message, conversation_history=history)


def _activate_fallback_stub(agent, *, succeeds=True):
    """Replace ``_try_activate_fallback`` with a recording stub.

    On success it mimics the real activation: advance the chain index, flip
    ``_fallback_activated``, and swap the provider so the next request goes to the
    other leg.
    """
    calls = {"count": 0}

    def _stub(reason=None):
        calls["count"] += 1
        if not succeeds:
            # The real implementation walks the chain and leaves the index past its
            # end when nothing could be activated.
            agent._fallback_index = len(agent._fallback_chain)
            return False
        agent._fallback_index += 1
        agent._fallback_activated = True
        agent.provider = "fallback-provider"
        agent.model = "fallback-model"
        return True

    agent._try_activate_fallback = _stub
    return calls


# ---------------------------------------------------------------------------
# 1. Escalation succeeds — the turn continues on the fallback leg
# ---------------------------------------------------------------------------


class TestThinkingExhaustionEscalates:
    def test_fallback_is_activated_and_the_turn_continues(self, loop_agent):
        """A live fallback leg must be used instead of losing the turn."""
        loop_agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
        loop_agent._fallback_index = 0
        calls = _activate_fallback_stub(loop_agent)

        loop_agent.client.chat.completions.create.side_effect = [
            _thinking_only_length_response(),
            _full_response("The fallback leg answered."),
        ]
        result = _run(loop_agent, "write me a long report")

        assert calls["count"] == 1, "the fallback must be attempted on thinking exhaustion"
        assert result["completed"] is True
        assert "fallback leg answered" in (result["final_response"] or "")

    def test_the_retry_goes_out_on_the_fallback_leg(self, loop_agent):
        """The re-issued call must actually hit the fallback provider."""
        loop_agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
        loop_agent._fallback_index = 0
        _activate_fallback_stub(loop_agent)

        loop_agent.client.chat.completions.create.side_effect = [
            _thinking_only_length_response(),
            _full_response("answered on the other leg"),
        ]
        _run(loop_agent, "write me a long report")

        assert loop_agent.provider == "fallback-provider"
        assert loop_agent.model == "fallback-model"

    def test_partial_content_is_rolled_back_to_the_last_clean_turn(self, loop_agent):
        """The fallback must get a coherent continuation point, not the partial."""
        loop_agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
        loop_agent._fallback_index = 0
        _activate_fallback_stub(loop_agent)

        loop_agent.client.chat.completions.create.side_effect = [
            _thinking_only_length_response(),
            _full_response("clean answer"),
        ]
        result = _run(loop_agent, "write me a long report")

        # The aborted turn's assistant row must not be in the transcript.
        assistant_rows = [
            m for m in result["messages"]
            if m.get("role") == "assistant" and "burned the whole budget" in (m.get("content") or "")
        ]
        assert assistant_rows == [], (
            "the thinking-only fragment must not survive into the fallback's transcript"
        )

    def test_continuation_counters_are_reset_for_the_fallback_leg(self, loop_agent):
        """The fallback starts with a fresh continuation budget."""
        loop_agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
        loop_agent._fallback_index = 0
        _activate_fallback_stub(loop_agent)

        loop_agent.client.chat.completions.create.side_effect = [
            _thinking_only_length_response(),
            _full_response("done"),
        ]
        result = _run(loop_agent, "write me a long report")

        assert result["completed"] is True
        # One truncated attempt + one successful re-issue on the fallback leg.
        assert len(loop_agent.client.chat.completions.create.call_args_list) == 2


# ---------------------------------------------------------------------------
# 2. No fallback available — the existing terminal behaviour is UNCHANGED
# ---------------------------------------------------------------------------


class TestThinkingExhaustionWithoutFallback:
    def test_empty_chain_keeps_the_existing_terminal_behaviour(self, loop_agent):
        """No fallback leg -> the pre-existing terminal result, message unchanged."""
        loop_agent._fallback_chain = []
        loop_agent._fallback_index = 0
        calls = _activate_fallback_stub(loop_agent)

        loop_agent.client.chat.completions.create.side_effect = [_thinking_only_length_response()]
        result = _run(loop_agent, "write me a long report")

        assert calls["count"] == 0, "an empty chain must not attempt a fallback"
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["final_response"] is not None
        assert "Thinking Budget Exhausted" in result["final_response"]
        assert "/reasoning" in result["final_response"]
        assert "reasoning" in (result.get("error") or "").lower()
        assert "output tokens" in (result.get("error") or "").lower()

    def test_exhausted_chain_keeps_the_existing_terminal_behaviour(self):
        """A chain that is already walked out keeps the terminal behaviour.

        ``restore_primary_runtime`` re-arms the chain at the start of every turn, so
        the exhausted state is a MID-TURN one: the leg was already consumed earlier in
        the same turn. The helper must then leave the turn alone.
        """
        from agent.turn_retry_state import TurnRetryState
        from agent.turn_truncation import _Trunc, _thinking_exhausted_fallback

        agent = MagicMock()
        agent._fallback_chain = [{"provider": "fb", "model": "fb-model"}]
        agent._fallback_index = 1  # already past the end of the chain
        agent._fallback_activated = True
        agent._try_activate_fallback = MagicMock(side_effect=lambda *a, **k: False)

        st = _Trunc(
            agent=agent, response=MagicMock(), finish_reason="length",
            conversation_history=None, api_call_count=1, effective_task_id=None,
            current_turn_user_idx=0, messages=[{"role": "user", "content": "hi"}],
            length_continue_retries=0, truncated_response_parts=[],
            truncated_tool_call_retries=0, retry_count=0, compression_attempts=0,
        )
        verdict = _thinking_exhausted_fallback(st, TurnRetryState())

        assert verdict is None, "an exhausted chain must not report an escalation"
        agent._try_activate_fallback.assert_not_called()
        assert agent._fallback_index == 1, "the exhausted chain position is left as it was"

    def test_a_failed_activation_keeps_the_existing_terminal_behaviour(self, loop_agent):
        """A fallback that cannot be activated must NOT change the outcome."""
        loop_agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
        loop_agent._fallback_index = 0
        _activate_fallback_stub(loop_agent, succeeds=False)

        loop_agent.client.chat.completions.create.side_effect = [_thinking_only_length_response()]
        result = _run(loop_agent, "write me a long report")

        assert result["completed"] is False
        assert "Thinking Budget Exhausted" in result["final_response"]
        assert "/reasoning" in result["final_response"]

    def test_the_message_text_is_byte_identical_to_the_pre_change_copy(self, loop_agent):
        """The user-facing message must not drift."""
        from agent.turn_truncation import _THINKING_EXHAUSTED

        expected_line = (
            "💭 Reasoning exhausted the output token budget — no visible response was produced."
        )
        expected_body = (
            "⚠️ **Thinking Budget Exhausted**\n\nThe model used all its output tokens on reasoning "
            "and had none left for the actual response.\n\nTo fix this:\n"
            "→ Lower reasoning effort: `/reasoning low` or `/reasoning minimal`\n"
            "→ Or switch to a larger/non-reasoning model with `/model`"
        )
        expected_error = (
            "Model used all output tokens on reasoning with none left "
            "for the response. Try lowering reasoning effort or increasing max_tokens."
        )
        assert _THINKING_EXHAUSTED == (expected_line, expected_body, expected_error)

        loop_agent._fallback_chain = []
        loop_agent._fallback_index = 0
        loop_agent.client.chat.completions.create.side_effect = [_thinking_only_length_response()]
        result = _run(loop_agent, "write me a long report")
        assert result["final_response"] == expected_body
        assert result["error"] == expected_error


# ---------------------------------------------------------------------------
# 3. _fallback_index is never stranded by the new call site
# ---------------------------------------------------------------------------


class TestFallbackIndexNotStranded:
    def test_failed_activation_does_not_strand_the_index(self, loop_agent):
        """The chain position must survive a failed activation.

        ``_fallback_index`` is chain-global state. A failed activation leaves it past
        the end of the chain, which silently blocks every future fallback for the
        session (the bug class fixed in ``agent/agent_runtime_helpers.py``).
        """
        loop_agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
        loop_agent._fallback_index = 0
        _activate_fallback_stub(loop_agent, succeeds=False)

        loop_agent.client.chat.completions.create.side_effect = [_thinking_only_length_response()]
        _run(loop_agent, "write me a long report")

        assert loop_agent._fallback_index < len(loop_agent._fallback_chain), (
            "a failed fallback activation must not strand _fallback_index past the chain end"
        )

    def test_the_index_is_restored_even_mid_turn_on_a_fallback_leg(self):
        """The mid-turn case: the per-turn reset does NOT apply.

        ``restore_primary_runtime`` only resets the index when no fallback was
        activated. While a fallback leg is ACTIVE the index keeps its value across
        turns, so a failed activation there really would strand it. The new call
        site restores it explicitly.
        """
        from agent.turn_retry_state import TurnRetryState
        from agent.turn_truncation import _Trunc, _thinking_exhausted_fallback

        agent = MagicMock()
        agent._fallback_chain = [{"provider": "fb", "model": "fb-model"}]
        agent._fallback_index = 0
        agent._fallback_activated = True
        agent._try_activate_fallback = MagicMock(
            side_effect=lambda *a, **k: False
        )

        st = _Trunc(
            agent=agent, response=MagicMock(), finish_reason="length",
            conversation_history=None, api_call_count=1, effective_task_id=None,
            current_turn_user_idx=0, messages=[{"role": "user", "content": "hi"}],
            length_continue_retries=0, truncated_response_parts=[],
            truncated_tool_call_retries=0, retry_count=0, compression_attempts=0,
        )
        verdict = _thinking_exhausted_fallback(st, TurnRetryState())

        assert verdict is None, "a failed activation must not report an escalation"
        assert agent._fallback_index == 0, (
            "the chain index must be restored so a later attempt can still walk the chain"
        )

    def test_a_successful_activation_advances_the_index(self, loop_agent):
        """The happy path still consumes a chain slot (the normal contract)."""
        loop_agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
        loop_agent._fallback_index = 0
        _activate_fallback_stub(loop_agent)

        loop_agent.client.chat.completions.create.side_effect = [
            _thinking_only_length_response(),
            _full_response("ok"),
        ]
        _run(loop_agent, "write me a long report")

        assert loop_agent._fallback_index == 1


# ---------------------------------------------------------------------------
# 4. The repetition-loop abort is NOT escalated (unchanged behaviour)
# ---------------------------------------------------------------------------


class TestRepetitionAbortNotEscalated:
    def test_repetition_dominated_output_is_not_escalated(self, loop_agent):
        """A repetition loop is a MODEL failure: it keeps the terminal behaviour."""
        from tests.agent.test_run_agent import _mock_response

        loop_agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
        loop_agent._fallback_index = 0
        calls = _activate_fallback_stub(loop_agent)

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content="same thing " * 40, finish_reason="length"),
        ]
        result = _run(loop_agent, "write me a long report")

        assert calls["count"] == 0, "the repetition abort must not escalate to the fallback"
        assert result["completed"] is False
