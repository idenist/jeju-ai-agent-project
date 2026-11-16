"""Opt-in timings for local diagnosis. Log only durations and stage names."""
import os
from time import monotonic


def latency_logging_enabled():
    return os.getenv("JEJU_LATENCY_LOG") == "1"


def log_latency(stage, started, **details):
    if latency_logging_enabled():
        fields = " ".join(f"{key}={value}" for key, value in details.items())
        print(f"[jeju-latency] {stage} seconds={monotonic() - started:.3f} {fields}".rstrip(), flush=True)
