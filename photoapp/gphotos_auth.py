"""OAuth for the Google Photos export (ticket 210): the installed-app loopback flow with PKCE, a
token file, and access-token refresh. stdlib only; the HTTP transport is injectable so tests never
touch the network. Nothing here is imported by the web app's request paths except `Credentials`.

The client JSON is the file the Google Cloud console offers for a 'Desktop app' OAuth client
(`{"installed": {"client_id", "client_secret", "auth_uri", "token_uri", ...}}`). The token file keeps
only the refresh token and the last access token with its expiry. Neither file is ever logged.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SCOPES = (
    "https://www.googleapis.com/auth/photoslibrary.appendonly",            # upload, create albums
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",  # list our albums
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",   # add to our albums
)
EXPIRY_MARGIN = 60   # seconds: refresh this early


class AuthError(Exception):
  """Not configured, not authorized, or the grant is gone; the message says what to do."""


def urllib_post_form(url, fields, timeout=30):
  """POST form fields; returns (status, parsed JSON or {})."""
  req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
      status, raw = r.status, r.read()
  except urllib.error.HTTPError as e:
    status, raw = e.code, e.read()
  except (urllib.error.URLError, OSError) as e:
    raise AuthError(f"cannot reach Google's token endpoint: {e}") from e
  try:
    return status, json.loads(raw)
  except ValueError:
    return status, {}


def load_client(path):
  """(client_id, client_secret, auth_uri, token_uri) from the downloaded client JSON."""
  try:
    with open(path) as f:
      data = json.load(f)
  except OSError:
    raise AuthError(f"no OAuth client file at {path}: create a 'Desktop app' OAuth client in the "
                    "Google Cloud console and save its JSON there (docs/google-photos.md)")
  except ValueError as e:
    raise AuthError(f"{path} is not valid JSON: {e}")
  c = data.get("installed") or data.get("web") or {}
  try:
    return (c["client_id"], c["client_secret"],
            c.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth"),
            c.get("token_uri", "https://oauth2.googleapis.com/token"))
  except KeyError as e:
    raise AuthError(f"{path} lacks {e}; is it the 'Desktop app' client JSON?")


def _write_private(path, data):
  os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
  tmp = path + ".tmp"
  fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w") as f:
    json.dump(data, f)
  os.replace(tmp, path)


class Credentials:
  """The access token for the Library API, refreshed from the stored refresh token as needed."""

  def __init__(self, client_path, token_path, post=urllib_post_form, clock=time.time):
    self.client_path, self.token_path = client_path, token_path
    self._post, self._clock = post, clock
    self._lock = threading.Lock()

  def configured(self):
    return os.path.isfile(self.client_path) and os.path.isfile(self.token_path)

  def _read_token(self):
    try:
      with open(self.token_path) as f:
        return json.load(f)
    except (OSError, ValueError):
      raise AuthError("Google Photos is not authorized yet: run `python -m photoapp.google_auth`")

  def access_token(self):
    with self._lock:
      tok = self._read_token()
      if tok.get("access_token") and tok.get("expires_at", 0) > self._clock() + EXPIRY_MARGIN:
        return tok["access_token"]
      client_id, secret, _, token_uri = load_client(self.client_path)
      status, body = self._post(token_uri, {
          "client_id": client_id, "client_secret": secret,
          "refresh_token": tok.get("refresh_token", ""), "grant_type": "refresh_token"})
      if status != 200 or "access_token" not in body:
        if body.get("error") == "invalid_grant":
          raise AuthError("Google no longer accepts the stored authorization (it expired or was "
                          "revoked): run `python -m photoapp.google_auth` again")
        raise AuthError(f"refreshing the Google access token failed ({status}): "
                        f"{body.get('error_description') or body.get('error') or 'no detail'}")
      tok["access_token"] = body["access_token"]
      tok["expires_at"] = self._clock() + int(body.get("expires_in", 3600))
      _write_private(self.token_path, tok)
      return tok["access_token"]


def _pkce():
  verifier = secrets.token_urlsafe(64)
  challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
  return verifier, challenge.decode()


class _Catcher(http.server.BaseHTTPRequestHandler):
  def do_GET(self):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    self.server.result = {k: v[0] for k, v in q.items()}
    self.send_response(200)
    self.send_header("Content-Type", "text/plain; charset=utf-8")
    self.end_headers()
    self.wfile.write(b"Authorization received. You can close this tab and return to the terminal.")

  def log_message(self, *a):
    pass


def authorize(client_path, token_path, show_url, post=urllib_post_form, clock=time.time,
              timeout=300):
  """Run the loopback flow: show_url(url) is told where to send the user's browser; the redirect
  lands on 127.0.0.1:<random port>. Stores the refresh token. Returns the token dict."""
  client_id, secret, auth_uri, token_uri = load_client(client_path)
  verifier, challenge = _pkce()
  state = secrets.token_urlsafe(16)
  server = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
  server.result, server.timeout = None, timeout
  redirect = f"http://127.0.0.1:{server.server_port}"
  show_url(auth_uri + "?" + urllib.parse.urlencode({
      "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
      "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
      "code_challenge": challenge, "code_challenge_method": "S256"}))
  deadline = clock() + timeout
  while server.result is None and clock() < deadline:
    server.handle_request()
  server.server_close()
  res = server.result
  if not res:
    raise AuthError("timed out waiting for the browser authorization")
  if res.get("error"):
    raise AuthError(f"Google refused the authorization: {res['error']}")
  if res.get("state") != state or "code" not in res:
    raise AuthError("the browser callback did not match this request; try again")
  status, body = post(token_uri, {
      "client_id": client_id, "client_secret": secret, "code": res["code"],
      "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"})
  if status != 200 or "refresh_token" not in body:
    raise AuthError(f"exchanging the code failed ({status}): "
                    f"{body.get('error_description') or body.get('error') or 'no refresh token'}")
  tok = {"refresh_token": body["refresh_token"], "access_token": body.get("access_token"),
         "expires_at": clock() + int(body.get("expires_in", 3600)), "scope": body.get("scope")}
  _write_private(token_path, tok)
  return tok
