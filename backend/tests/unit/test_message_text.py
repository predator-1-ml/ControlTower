"""`message_text` normalises model responses across providers.

`.content` is not reliably a string, and the failure mode is silent: Gemini
returns a list of content blocks, and using `.content` directly puts that list —
including a multi-kilobyte `signature` blob — into graph state, where it is
checkpointed to Postgres and serialised into every SSE frame. Nothing raises.

Found by running the stack against Gemini and looking at what actually reached
the browser.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.provider import message_text


@dataclass
class Response:
    content: object


def test_plain_string_passes_through():
    """Anthropic and Bedrock return a bare string."""
    assert message_text(Response("hello")) == "hello"


def test_gemini_content_blocks_are_flattened():
    """The real shape observed from ChatGoogleGenerativeAI."""
    response = Response(
        [
            {
                "type": "text",
                "text": "The onboarding outcome is manual review.",
                "index": 0,
                "extras": {"signature": "EsYVCsMVARFNMg9+lH" * 200},
            }
        ]
    )
    text = message_text(response)

    assert text == "The onboarding outcome is manual review."
    # The signature blob must not survive: it would be checkpointed and streamed.
    assert "EsYVCsMV" not in text


def test_multiple_blocks_are_concatenated_in_order():
    response = Response(
        [
            {"type": "text", "text": "First. "},
            {"type": "text", "text": "Second."},
        ]
    )
    assert message_text(response) == "First. Second."


def test_non_text_blocks_are_dropped():
    """Thinking and tool-use blocks are not the answer and must not be shown."""
    response = Response(
        [
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "The visible answer."},
        ]
    )
    assert message_text(response) == "The visible answer."


def test_bare_strings_inside_the_list_are_kept():
    assert message_text(Response(["a", "b"])) == "ab"


def test_unexpected_shape_degrades_to_str_rather_than_raising():
    """A provider returning something unforeseen must not break a graph run."""
    assert message_text(Response(42)) == "42"


def test_object_without_content_is_handled():
    """Defensive: some call sites pass the response, some could pass content."""
    assert message_text("already a string") == "already a string"
