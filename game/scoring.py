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


def partial_score(answer: str, guess: str) -> int:
    """
    Return a partial score based on positional character matches.

    Score = floor(100 * positional_char_matches / len(answer))
    Positional match: answer[i] == guess[i] for each i up to min(len(answer), len(guess)).
    Case-insensitive comparison.
    Returns 0 if answer is empty.
    """
    if not answer:
        return 0
    answer_lower = answer.lower()
    guess_lower = guess.lower()
    matches = sum(
        1 for i in range(min(len(answer_lower), len(guess_lower)))
        if answer_lower[i] == guess_lower[i]
    )
    return int(100 * matches / len(answer_lower))


def setter_penalty() -> int:
    """Return the penalty applied to the setter when nobody guesses correctly."""
    return SETTER_PENALTY
