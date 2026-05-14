from __future__ import annotations

ROUND_GUESSER_SCORES: dict[int, int] = {1: 100, 2: 80, 3: 60, 4: 40, 5: 0}
ROUND_SETTER_SCORES: dict[int, int]  = {1: 20,  2: 40, 3: 60, 4: 80, 5: 100}
SETTER_PENALTY: int = -100


def guesser_score(clue_round: int) -> int:
    """Return the guesser's score for a correct answer at the given clue round."""
    return ROUND_GUESSER_SCORES[clue_round]


def setter_score_if_guessed(clue_round: int) -> int:
    """Return the setter's score when someone guesses correctly at the given clue round."""
    return ROUND_SETTER_SCORES[clue_round]


def count_char_matches(answer: str, guess: str) -> int:
    """Count how many characters in answer appear anywhere in guess (position-independent)."""
    if not answer:
        return 0
    guess_chars = set(guess.lower())
    return sum(1 for ch in answer.lower() if ch in guess_chars)


def partial_score(answer: str, guess: str) -> int:
    """
    Return a partial score based on position-independent character matches.

    Score = floor(100 * matched_chars / len(answer))
    A char in answer is matched if it appears anywhere in guess (position doesn't matter).
    Case-insensitive. Returns 0 if answer is empty.
    """
    if not answer:
        return 0
    matches = count_char_matches(answer, guess)
    return int(100 * matches / len(answer))


def setter_penalty() -> int:
    """Return the penalty applied to the setter when nobody guesses correctly."""
    return SETTER_PENALTY
