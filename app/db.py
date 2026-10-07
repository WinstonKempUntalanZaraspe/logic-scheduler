"""In-memory duration-learning adapter for the public logic lab."""
_DURATION_MULTIPLIERS: dict[str, float] = {}

def set_duration_multiplier(category: str, value: float) -> None:
    _DURATION_MULTIPLIERS[str(category)] = max(0.75, min(2.5, float(value)))

def duration_multiplier(category: str) -> float:
    return _DURATION_MULTIPLIERS.get(str(category), 1.0)
