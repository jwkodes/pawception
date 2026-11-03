"""Reasoning levels supported by the Muse models used in the monitor."""

EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def validate_effort(value, model):
    allowed = (*EFFORTS, "max") if model == "muse-spark-1.3" else EFFORTS
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"Reasoning effort for {model} must be one of: {', '.join(allowed)}")
    return value
