"""Deterministic demonstration agents. Replace with your actual agent adapters."""

def echo(value, context):
    return value


def uppercase(value, context):
    if not isinstance(value, str):
        raise ValueError("uppercase expects a text input")
    return value.upper()


def summarize(value, context):
    # Demo only. This does NOT invoke an LLM or perform intelligent summarization.
    text = value if isinstance(value, str) else str(value)
    return " ".join(text.split())[:120]


def conversation_probe(value, context):
    """Deterministic test/demo adapter showing history arrives across turns."""
    history = context.get("_mesh_history", [])
    return {"received": value, "previous_speakers": [item["sender"] for item in history],
            "conversation_id": context.get("_mesh_conversation_id")}


def fail_demo(value, context):
    raise RuntimeError("Simulated agent failure; do not expose internal error details")


def slow_demo(value, context):
    """Testing-only long running call for SIGKILL/crash recovery exercise."""
    import time
    time.sleep(3)
    return value
