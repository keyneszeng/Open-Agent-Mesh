"""Deterministic local chaos test handler; NOT intended for production use."""
import time

def slow_echo(value, context):
    time.sleep(2.5)
    return value
