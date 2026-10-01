"""Minimal Gemini client for AI rating (ticket 177): one image + one prompt in, a dict of validated
integer answers out. stdlib only; the transport is injectable so tests never touch the network."""

import base64
import json
import time
import urllib.error
import urllib.request

from absl import logging

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRY_STATUSES = (429, 500, 502, 503, 504)


class GeminiError(Exception):
  pass


def urllib_transport(url, headers, body, timeout=120):
  """POST body; returns (status, response bytes) for any HTTP status."""
  req = urllib.request.Request(url, data=body, headers=headers, method="POST")
  try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
      return r.status, r.read()
  except urllib.error.HTTPError as e:
    return e.code, e.read()
  except (urllib.error.URLError, OSError) as e:
    raise GeminiError(f"cannot reach the Gemini API: {e}") from e


def build_request(model, prompt, image_bytes, mime):
  body = {
      "contents": [{"parts": [
          {"text": prompt.text},
          {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}}]}],
      "generationConfig": {"responseMimeType": "application/json",
                           "responseSchema": prompt.schema, "temperature": 0},
  }
  return ENDPOINT.format(model=model), json.dumps(body).encode()


def _error_message(raw):
  try:
    return json.loads(raw)["error"]["message"]
  except (ValueError, KeyError, TypeError):
    return raw[:200].decode(errors="replace")


def parse_answers(response, schema):
  """The answers dict from a generateContent response, validated against the schema."""
  try:
    text = response["candidates"][0]["content"]["parts"][0]["text"]
  except (KeyError, IndexError, TypeError):
    reason = (response.get("promptFeedback") or {}).get("blockReason") if isinstance(
        response, dict) else None
    raise GeminiError("no answer in the response" + (f" (blocked: {reason})" if reason else ""))
  try:
    answers = json.loads(text)
  except ValueError as e:
    raise GeminiError(f"answer is not JSON: {text[:100]!r}") from e
  if not isinstance(answers, dict):
    raise GeminiError("answer is not a JSON object")
  for key in schema.get("required", []):
    spec = schema["properties"][key]
    value = answers.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
      raise GeminiError(f"answer {key!r} is missing or not an integer: {value!r}")
    if not spec.get("minimum", value) <= value <= spec.get("maximum", value):
      raise GeminiError(f"answer {key!r}={value} is outside {spec.get('minimum')}..{spec.get('maximum')}")
  return {key: answers[key] for key in schema.get("required", [])}


def rate_image(api_key, model, prompt, image_bytes, mime="image/jpeg", transport=None,
               sleep=time.sleep, retries=2):
  """Send the image with the prompt; returns {question key: int}. Raises GeminiError."""
  transport = transport or urllib_transport
  url, body = build_request(model, prompt, image_bytes, mime)
  headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}   # key never in the URL
  for attempt in range(retries + 1):
    status, raw = transport(url, headers, body)
    if status in RETRY_STATUSES and attempt < retries:
      logging.vlog(1, "gemini: HTTP %d, retrying", status)
      sleep(2 ** attempt * 2)
      continue
    break
  if status != 200:
    raise GeminiError(f"Gemini API error {status}: {_error_message(raw)}")
  try:
    response = json.loads(raw)
  except ValueError as e:
    raise GeminiError("response is not JSON") from e
  return parse_answers(response, prompt.schema)
