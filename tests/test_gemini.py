import base64
import json

import pytest

from photoapp import ai_prompts
from photoapp import gemini

SCHEMA = {"type": "OBJECT",
          "properties": {"a": {"type": "INTEGER", "minimum": 0, "maximum": 5},
                         "f": {"type": "INTEGER", "minimum": -2, "maximum": 2}},
          "required": ["a", "f"]}
PROMPT = ai_prompts.make("t", "rate", SCHEMA)


def reply(answers, status=200):
  text = answers if isinstance(answers, str) else json.dumps(answers)
  body = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
  return status, json.dumps(body).encode()


class Fake:
  def __init__(self, *replies):
    self.replies, self.calls = list(replies), []

  def __call__(self, url, headers, body):
    self.calls.append((url, headers, json.loads(body)))
    return self.replies.pop(0)


def test_request_shape_and_parsed_answers():
  t = Fake(reply({"a": 4, "f": -1, "extra": 9}))
  out = gemini.rate_image("KEY", "m1", PROMPT, b"JPEGDATA", transport=t)
  assert out == {"a": 4, "f": -1}
  url, headers, body = t.calls[0]
  assert url.endswith("/models/m1:generateContent") and "KEY" not in url
  assert headers["x-goog-api-key"] == "KEY"
  parts = body["contents"][0]["parts"]
  assert parts[0] == {"text": "rate"}
  assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"JPEGDATA"
  cfg = body["generationConfig"]
  assert cfg["responseSchema"] == SCHEMA and cfg["responseMimeType"] == "application/json"


@pytest.mark.parametrize("answers", [{"a": 6, "f": 0}, {"a": 1}, {"a": "3", "f": 0},
                                     {"a": True, "f": 0}, "not json", [1, 2]])
def test_invalid_answers_raise(answers):
  with pytest.raises(gemini.GeminiError):
    gemini.rate_image("K", "m", PROMPT, b"x", transport=Fake(reply(answers)))


def test_blocked_response_names_the_reason():
  body = json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}).encode()
  with pytest.raises(gemini.GeminiError, match="SAFETY"):
    gemini.rate_image("K", "m", PROMPT, b"x", transport=Fake((200, body)))


def test_429_is_retried_then_succeeds():
  slept = []
  t = Fake((429, b"{}"), reply({"a": 1, "f": 0}))
  assert gemini.rate_image("K", "m", PROMPT, b"x", transport=t, sleep=slept.append) == {"a": 1, "f": 0}
  assert len(t.calls) == 2 and slept


def test_http_error_carries_the_api_message_not_the_key():
  err = json.dumps({"error": {"message": "API key not valid"}}).encode()
  with pytest.raises(gemini.GeminiError, match="400.*API key not valid") as e:
    gemini.rate_image("SECRET", "m", PROMPT, b"x", transport=Fake((400, err)))
  assert "SECRET" not in str(e.value)
