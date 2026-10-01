"""Turns the structured AI answers (prompts/rating.md) into one number, 0 to 10 (ticket 178).

Edit WEIGHTS or score() freely: VERSION is the hash of this very file, stored next to every computed
score, so any edit makes the stored scores stale and ai_rating.rescore_stale() recomputes them from
the cached answers (no new request to Gemini). Keep this module dependency-free for that reason.
"""

import hashlib
import os

# Each 0..5 answer contributes weight * answer / 5; the weights of the 0..5 questions sum to 10.
WEIGHTS = {
    "sharpness": 3.0,
    "composition": 2.5,
    "exposure": 2.0,
    "subject_interest": 1.5,
    "color": 1.0,
}
FACES_BONUS = 1.0        # faces is -2..2; counted (as +-FACES_BONUS) only when there are people
FLAWS_PENALTY = 1.5      # technical_flaws is 0..5; subtracts up to this much


def score(answers):
  """answers: {question key: int} as returned by gemini.rate_image. Returns a float in [0, 10]."""
  total = sum(w * answers[k] / 5 for k, w in WEIGHTS.items())
  if answers["has_people"]:
    total += FACES_BONUS * answers["faces"] / 2
  total -= FLAWS_PENALTY * answers["technical_flaws"] / 5
  return round(min(10.0, max(0.0, total)), 2)


def _own_version():
  with open(os.path.abspath(__file__), "rb") as f:
    return hashlib.sha256(f.read()).hexdigest()[:12]


VERSION = _own_version()
