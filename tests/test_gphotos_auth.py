import json
import os
import stat
import threading
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient

from photoapp import api
from photoapp import db
from photoapp import gphotos_auth


def write_client(path, token_uri="https://token.example/token"):
  with open(path, "w") as f:
    json.dump({"installed": {"client_id": "cid", "client_secret": "sec",
                             "auth_uri": "https://auth.example/auth", "token_uri": token_uri}}, f)


class FakeToken:
  def __init__(self, status=200, body=None):
    self.calls, self.status = [], status
    self.body = body if body is not None else {"access_token": "AT2", "expires_in": 3600}

  def __call__(self, url, fields):
    self.calls.append((url, fields))
    return self.status, self.body


def test_load_client_reads_the_installed_section_and_explains_a_missing_file(tmp_path):
  p = str(tmp_path / "c.json")
  write_client(p)
  assert gphotos_auth.load_client(p)[:2] == ("cid", "sec")
  with pytest.raises(gphotos_auth.AuthError, match="Cloud console"):
    gphotos_auth.load_client(str(tmp_path / "nope.json"))


def test_a_fresh_access_token_is_used_without_calling_google(tmp_path):
  c, t = str(tmp_path / "c.json"), str(tmp_path / "t.json")
  write_client(c)
  json.dump({"refresh_token": "R", "access_token": "AT", "expires_at": 10_000}, open(t, "w"))
  post = FakeToken()
  cred = gphotos_auth.Credentials(c, t, post=post, clock=lambda: 1000)
  assert cred.access_token() == "AT" and post.calls == []


def test_an_expired_access_token_is_refreshed_and_saved_privately(tmp_path):
  c, t = str(tmp_path / "c.json"), str(tmp_path / "t.json")
  write_client(c)
  json.dump({"refresh_token": "R", "access_token": "old", "expires_at": 5}, open(t, "w"))
  post = FakeToken()
  cred = gphotos_auth.Credentials(c, t, post=post, clock=lambda: 1000)
  assert cred.access_token() == "AT2"
  url, fields = post.calls[0]
  assert url == "https://token.example/token"
  assert fields["grant_type"] == "refresh_token" and fields["refresh_token"] == "R"
  saved = json.load(open(t))
  assert saved["access_token"] == "AT2" and saved["refresh_token"] == "R"
  assert stat.S_IMODE(os.stat(t).st_mode) == 0o600
  assert cred.access_token() == "AT2" and len(post.calls) == 1


def test_a_revoked_grant_says_to_run_google_auth_again(tmp_path):
  c, t = str(tmp_path / "c.json"), str(tmp_path / "t.json")
  write_client(c)
  json.dump({"refresh_token": "R", "expires_at": 0}, open(t, "w"))
  cred = gphotos_auth.Credentials(c, t, post=FakeToken(400, {"error": "invalid_grant"}))
  with pytest.raises(gphotos_auth.AuthError, match="google_auth"):
    cred.access_token()


def test_no_token_file_means_not_authorized(tmp_path):
  c = str(tmp_path / "c.json")
  write_client(c)
  cred = gphotos_auth.Credentials(c, str(tmp_path / "missing.json"))
  assert not cred.configured()
  with pytest.raises(gphotos_auth.AuthError, match="google_auth"):
    cred.access_token()


def run_flow(tmp_path, reply, post):
  c, t = str(tmp_path / "c.json"), str(tmp_path / "t.json")
  write_client(c)
  seen = {}

  def show(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert "photoslibrary.appendonly" in q["scope"][0]
    seen["state"] = q["state"][0]

  gphotos_auth.authorize(c, t, show, lambda: reply(seen["state"]), post=post)
  return t


@pytest.mark.parametrize("reply", [
    lambda st: "CODE",
    lambda st: f"http://127.0.0.1:1/?state={st}&code=CODE&scope=x",
    lambda st: f"state={st}&code=CODE"])
def test_the_manual_flow_stores_a_refresh_token(tmp_path, reply):
  post = FakeToken(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
  t = run_flow(tmp_path, reply, post)
  fields = post.calls[0][1]
  assert fields["code"] == "CODE" and fields["grant_type"] == "authorization_code"
  assert fields["code_verifier"] and fields["redirect_uri"].startswith("http://127.0.0.1:")
  saved = json.load(open(t))
  assert saved["refresh_token"] == "RT" and stat.S_IMODE(os.stat(t).st_mode) == 0o600


def test_a_web_client_uses_its_registered_https_redirect(tmp_path):
  c = str(tmp_path / "c.json")
  json.dump({"web": {"client_id": "i", "client_secret": "s",
                     "redirect_uris": ["https://example.org/callback"]}}, open(c, "w"))
  assert gphotos_auth.redirect_uri(c) == "https://example.org/callback"
  write_client(c)
  assert gphotos_auth.redirect_uri(c).startswith("http://127.0.0.1")


def test_a_pasted_url_with_the_wrong_state_is_rejected(tmp_path):
  with pytest.raises(gphotos_auth.AuthError, match="did not match"):
    run_flow(tmp_path, lambda st: "http://127.0.0.1:1/?code=X&state=forged", FakeToken())
  assert not os.path.exists(str(tmp_path / "t.json"))


def test_status_endpoint_reflects_the_files(settings):
  conn = db.open_state(settings.state_dir)
  c = TestClient(api.create_app(conn, settings))
  r = c.get("/api/gphotos/status").json()
  assert r["configured"] is False and "google_auth" in r["hint"]
  write_client(settings.gphotos_client_path)
  json.dump({"refresh_token": "R"}, open(settings.gphotos_token_path, "w"))
  assert c.get("/api/gphotos/status").json() == {"configured": True, "hint": None}
