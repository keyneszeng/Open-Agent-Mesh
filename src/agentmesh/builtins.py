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
