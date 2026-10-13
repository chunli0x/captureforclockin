"""Judge accuracy ladder (parallel to the capture `leveling.py`).

Keyed on *agreement_rate only* — the fraction of a judge's resolved judgments
they aligned with the crowd. This is a different meaning from the capture
ladder (which gates on accepted-count + acceptance-rate), so it lives in its
own module and must not reuse `leveling.py`'s table.

Payout is ``judge_base_skr × multiplier`` per aligned judgment. Status (level)
is free; only the multiplier costs money.
"""

from typing import Final

MAX_LEVEL: Final = 4

# level -> (name, rate_threshold | None, rate_multiplier)
LEVELS: Final[list[tuple[int, str, float | None, float]]] = [
    (0, "Scout", None, 1.00),
    (1, "Regular", 0.80, 1.10),
    (2, "Veteran", 0.88, 1.25),
    (3, "Expert", 0.93, 1.50),
    (4, "Pathfinder", 0.97, 2.00),
]


def level_for(agreement_rate: float) -> int:
    """Highest level whose agreement-rate gate is met. Level 0 has no gate."""
    level = 0
    for lv, _name, threshold, _mult in LEVELS:
        if threshold is not None and agreement_rate < threshold:
            continue
        level = lv
    return level


def rate_multiplier(level: int) -> float:
    return LEVELS[max(0, min(level, MAX_LEVEL))][3]


def level_info(agreement_rate: float) -> dict:
    """The judge level block, matching /judge/stats field names."""
    level = level_for(agreement_rate)
    _lv, name, _threshold, mult = LEVELS[level]
    return {
        "level": level,
        "level_name": name,
        "agreement_rate": agreement_rate,
        "rate_multiplier": mult,
    }
