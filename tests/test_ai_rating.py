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


def test_ai_rate_endpoint_queues_representative_files(settings, conn, monkeypatch):
  from fastapi.testclient import TestClient
  from photoapp import api
  for n in "ab":
    make_jpeg(os.path.join(settings.pictures_dir, f"{n}.jpg"), size=(300, 200), color=n == "a" and "red" or "blue")
  scan.scan(conn, settings.pictures_dir)
  pids = [r[0] for r in conn.execute("SELECT id FROM photos ORDER BY id")]
  monkeypatch.delenv("GEMINI_API_KEY", raising=False)
  c = TestClient(api.create_app(conn, settings))
  r = c.post("/api/ai/rate", json={"ids": pids})
  assert r.status_code == 400 and "API key" in r.json()["detail"]
  monkeypatch.setenv("GEMINI_API_KEY", "K")
  r = c.post("/api/ai/rate", json={"ids": pids + [9999]})
  assert r.json() == {"queued": 2, "missing": 1}
  c.post("/api/ai/rate", json={"ids": pids})                    # deduped while queued
  assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai_rate'").fetchone()[0] == 2
  assert c.post("/api/ai/rate", json={"ids": []}).status_code == 400
