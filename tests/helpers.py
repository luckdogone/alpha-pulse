from anthropic.types import Message


def response(content, stop="end_turn"):
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-opus-5",
        content=content,
        stop_reason=stop,
        stop_sequence=None,
        usage={"input_tokens": 100, "output_tokens": 30},
    )
