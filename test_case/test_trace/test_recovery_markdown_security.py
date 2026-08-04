from agent.trace.recovery_markdown import _event_payload_str


def test_recovery_payload_is_markdown_and_html_escaped():
    rendered = _event_payload_str(
        {"message": "<img src=x onerror=alert(1)>", "link": "[click](javascript:x)"}
    )

    assert "<img" not in rendered
    assert "&lt;img" in rendered
    assert "\\[click\\]" in rendered
