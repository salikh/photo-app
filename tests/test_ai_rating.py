import dataclasses
import json
import os

import pytest

from photoapp import ai_prompts
from photoapp import ai_rating
from photoapp import ai_score
from photoapp import scan
from tests.conftest import make_jpeg

ANSWERS = {"sharpness": 4, "composition": 3, "exposure": 5, "has_people": 1, "faces": 1,
           "subject_interest": 2, "color": 3, "technical_flaws": 1}


class Fake:
  def __init__(self):
    self.calls = 0

  def __call__(self, url, headers, body):
    self.calls += 1
    text = json.dumps(ANSWERS)
    return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode()


@pytest.fixture
def setup(conn, settings, tmp_path, monkeypatch):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(300, 200))
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  key = tmp_path / "key"
  key.write_text("K")
  monkeypatch.delenv("GEMINI_API_KEY", raising=False)
  s = dataclasses.replace(settings, gemini_api_key_file=str(key))
  return conn, s, fid


def score_of(conn, fid):
  return conn.execute("SELECT ai_score, ai_score_version FROM files WHERE id=?", (fid,)).fetchone()


def test_first_run_sends_second_uses_cache(setup):
  conn, s, fid = setup
  t = Fake()
  assert ai_rating.rate_file(conn, s, fid, transport=t) is True
  assert ai_rating.rate_file(conn, s, fid, transport=t) is False
  assert t.calls == 1
  assert tuple(score_of(conn, fid)) == (ai_score.score(ANSWERS), ai_score.VERSION)
  assert conn.execute("SELECT COUNT(*) FROM ai_prompts").fetchone()[0] == 1


def test_edited_prompt_or_model_sends_again(setup, monkeypatch):
  conn, s, fid = setup
  t = Fake()
  ai_rating.rate_file(conn, s, fid, transport=t)
  p = ai_rating.current_prompt()
  edited = ai_prompts.make(p.name, p.text + "\nBe harsh.", p.schema)
  monkeypatch.setattr(ai_rating, "current_prompt", lambda: edited)
  assert ai_rating.rate_file(conn, s, fid, transport=t) is True
  assert ai_rating.rate_file(conn, s, fid, transport=t) is False
  assert t.calls == 2
  assert ai_rating.rate_file(conn, dataclasses.replace(s, ai_model="other"), fid, transport=t) is True
  assert t.calls == 3


def test_edited_scoring_function_rescores_without_sending(setup, monkeypatch):
  conn, s, fid = setup
  t = Fake()
  ai_rating.rate_file(conn, s, fid, transport=t)
  assert ai_rating.rescore_stale(conn, s) == 0                   # nothing stale
  monkeypatch.setattr(ai_score, "VERSION", "newversion")
  monkeypatch.setattr(ai_score, "score", lambda a: 1.25)
  assert ai_rating.rescore_stale(conn, s) == 1
  assert tuple(score_of(conn, fid)) == (1.25, "newversion")
  assert t.calls == 1


def test_no_key_fails_on_miss_but_not_on_hit(setup):
  conn, s, fid = setup
  nokey = dataclasses.replace(s, gemini_api_key_file=s.state_dir + "/nope")
  with pytest.raises(ai_rating.AiRatingError, match="API key"):
    ai_rating.rate_file(conn, nokey, fid, transport=Fake())
  ai_rating.rate_file(conn, s, fid, transport=Fake())
  assert ai_rating.rate_file(conn, nokey, fid, transport=Fake()) is False


def test_job_runs_through_the_queue(settings, conn, tmp_path, monkeypatch):
  from photoapp import api
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(300, 200))
  scan.scan(conn, settings.pictures_dir)
  monkeypatch.setenv("GEMINI_API_KEY", "K")
  app = api.create_app(conn, settings)
  t = app.state.gemini_transport = Fake()
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  app.state.jobs.enqueue("ai_rate", file_id=fid)
  app.state.jobs.start()
  try:
    assert app.state.jobs.wait_idle()
  finally:
    app.state.jobs.stop()
  assert t.calls == 1 and score_of(conn, fid)["ai_score"] == ai_score.score(ANSWERS)
