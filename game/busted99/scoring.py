from __future__ import annotations


def score_for_space(space: int) -> int:
    """
    Calculate score based on the remaining search space.

    space = high_bound - low_bound + 1 (number of possible values in range)

    | space | score |
    |-------|-------|
    | 90-99 | 10    |
    | 80-89 | 20    |
    | 70-79 | 30    |
    | 60-69 | 40    |
    | 50-59 | 50    |
    | 40-49 | 60    |
    | 30-39 | 70    |
    | 20-29 | 80    |
    | 10-19 | 90    |
    | 1-9   | 100   |
    """
    return max(10, min(100, 100 - (space // 10) * 10))
