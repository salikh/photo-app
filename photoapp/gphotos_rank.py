"""Ranking Google Photos albums against the album name an export is about to use (ticket 213)."""

import difflib
import re

_YEAR = re.compile(r"(?<!\d)(19|20)\d\d(?!\d)")


def _norm(text):
  return " ".join(re.findall(r"\w+", text.casefold()))


def score(title, name):
  """0..1: text similarity, plus a bonus when both mention the same year, minus nothing else."""
  t, n = _norm(title), _norm(name)
  if not n:
    return 0.0
  s = difflib.SequenceMatcher(None, t, n).ratio()
  if t == n:
    return 1.0
  years_t = {m.group(0) for m in _YEAR.finditer(title)}
  years_n = {m.group(0) for m in _YEAR.finditer(name)}
  if years_t & years_n:
    s = min(0.99, s + 0.2)
  return s


def rank(albums, name, limit=50):
  """albums: [{"id", "title", "count"}]. Best match first (ties: the larger album, then title);
  each gets "score" and "exact" (same title ignoring case, spacing and punctuation)."""
  out = []
  for a in albums:
    out.append({**a, "score": round(score(a["title"], name), 3),
                "exact": bool(name) and _norm(a["title"]) == _norm(name)})
  out.sort(key=lambda a: (-a["score"], -a["count"], a["title"]))
  return out[:limit]
