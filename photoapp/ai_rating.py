"""AI rating of a file: cache lookup, Gemini request on a miss, score computation (ticket 179).

Two content hashes decide what is recomputed: the prompt hash (plus model) is part of the response
cache key, so a changed prompt re-sends the picture; ai_score.VERSION is stored with each score, so
a changed scoring function only rescores from the cached answers, with no request at all.
"""

import datetime
import json

from absl import logging

from photoapp import ai_prompts
from photoapp import ai_score
from photoapp import gemini
from photoapp import thumbs

PROMPT_NAME = "rating"


class AiRatingError(Exception):
  pass


def image_key(file_hash, thumb_rev, file_id):
  """What identifies the rendition that is sent: content + render revision (crop/RAW tuning)."""
  return f"{file_hash}:{thumb_rev}" if file_hash else f"id{file_id}:{thumb_rev}"


def current_prompt():
  return ai_prompts.load(PROMPT_NAME)


def cached_answers(conn, key, prompt_hash, model):
  row = conn.execute(
      "SELECT answers FROM ai_responses WHERE image_key = ? AND prompt_hash = ? AND model = ?",
      (key, prompt_hash, model)).fetchone()
  return json.loads(row["answers"]) if row else None


def _store_score(conn, file_id, answers):
  conn.execute("UPDATE files SET ai_score = ?, ai_score_version = ? WHERE id = ?",
               (ai_score.score(answers), ai_score.VERSION, file_id))


def rate_file(conn, settings, file_id, transport=None):
  """Rate one file. Returns True if a request was sent, False if the cached answers were used."""
  row = conn.execute("SELECT id, path, hash, thumb_rev FROM files WHERE id = ?",
                     (file_id,)).fetchone()
  if row is None:
    raise AiRatingError("file is gone")
  prompt = current_prompt()
  ai_prompts.register(conn, prompt)
  key = image_key(row["hash"], row["thumb_rev"], file_id)
  answers = cached_answers(conn, key, prompt.hash, settings.ai_model)
  sent = answers is None
  if sent:
    api_key = settings.gemini_api_key()
    if not api_key:
      raise AiRatingError("no Gemini API key configured (set $GEMINI_API_KEY or create "
                          "<state_dir>/gemini_api_key, see docs/operations.md)")
    medium = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, file_id,
                           row["path"], "Medium")
    if medium is None:
      raise AiRatingError("no Medium thumbnail could be made for this file")
    with open(medium, "rb") as f:
      image = f.read()
    answers = gemini.rate_image(api_key, settings.ai_model, prompt, image, transport=transport)
    conn.execute(
        "INSERT OR REPLACE INTO ai_responses (image_key, prompt_hash, model, answers, created_at)"
        " VALUES (?,?,?,?,?)",
        (key, prompt.hash, settings.ai_model, json.dumps(answers),
         datetime.datetime.now().isoformat(timespec="seconds")))
    logging.vlog(5, "%s: AI rated (prompt %s)", row["path"], prompt.hash)
  _store_score(conn, file_id, answers)
  conn.commit()
  return sent


def rescore_stale(conn, settings):
  """Recompute ai_score for every file whose stored score came from an older scoring function,
  from the cached answers for the current prompt and model (no network). Files without a cached
  response keep their old score until they are rated again. Returns how many were rescored."""
  prompt = current_prompt()
  n = 0
  rows = conn.execute(
      "SELECT id, hash, thumb_rev FROM files WHERE ai_score IS NOT NULL AND "
      "(ai_score_version IS NULL OR ai_score_version != ?)", (ai_score.VERSION,)).fetchall()
  for r in rows:
    answers = cached_answers(conn, image_key(r["hash"], r["thumb_rev"], r["id"]), prompt.hash,
                             settings.ai_model)
    if answers is not None:
      _store_score(conn, r["id"], answers)
      n += 1
  conn.commit()
  if n:
    logging.info("AI rating: rescored %d file(s) with scoring function %s", n, ai_score.VERSION)
  return n


def answers_for(conn, settings, file_row):
  """The cached answers for the current prompt/model of a files row (a dict with hash, thumb_rev,
  id), or None."""
  prompt = current_prompt()
  return cached_answers(conn, image_key(file_row["hash"], file_row["thumb_rev"], file_row["id"]),
                        prompt.hash, settings.ai_model)
