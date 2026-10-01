import hashlib

from photoapp import ai_score

BEST = {"sharpness": 5, "composition": 5, "exposure": 5, "has_people": 0, "faces": 0,
        "subject_interest": 5, "color": 5, "technical_flaws": 0}


def test_extremes_and_clamping():
  assert ai_score.score(BEST) == 10.0
  worst = {**BEST, "sharpness": 0, "composition": 0, "exposure": 0, "subject_interest": 0,
           "color": 0, "technical_flaws": 5, "has_people": 1, "faces": -2}
  assert ai_score.score(worst) == 0.0
  assert ai_score.score({**BEST, "has_people": 1, "faces": 2}) == 10.0     # clamped


def test_monotonic_in_sharpness():
  scores = [ai_score.score({**BEST, "sharpness": s, "technical_flaws": 2}) for s in range(6)]
  assert scores == sorted(scores) and len(set(scores)) == 6


def test_faces_only_count_with_people():
  base = {**BEST, "sharpness": 2}
  assert ai_score.score({**base, "faces": -2}) == ai_score.score({**base, "faces": 2})
  assert (ai_score.score({**base, "has_people": 1, "faces": 2})
          > ai_score.score({**base, "has_people": 1, "faces": -2}))


def test_version_is_the_hash_of_the_source():
  with open(ai_score.__file__, "rb") as f:
    assert ai_score.VERSION == hashlib.sha256(f.read()).hexdigest()[:12]
