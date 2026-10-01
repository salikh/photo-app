"""Content-hashed prompt store (ticket 175).

A prompt is a pair of plain files in photoapp/prompts/ (NAME.md, the text, and NAME.schema.json,
the Gemini response schema), edited like code. Its version is the hash of its content, so there is
no version counter to bump and no linear history: every use registers the content in the ai_prompts
table, which keeps the exact text behind any cached response.
"""

import dataclasses
import datetime
import hashlib
import json
import os

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


@dataclasses.dataclass(frozen=True)
class Prompt:
  name: str
  text: str
  schema: dict
  hash: str


def canonical_schema(schema):
  return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def content_hash(text, schema):
  """First 16 hex chars of sha256(text + separator + canonical schema JSON)."""
  data = text + "\n---\n" + canonical_schema(schema)
  return hashlib.sha256(data.encode()).hexdigest()[:16]


def make(name, text, schema):
  return Prompt(name, text, schema, content_hash(text, schema))


def load(name, prompts_dir=PROMPTS_DIR):
  with open(os.path.join(prompts_dir, name + ".md"), encoding="utf-8") as f:
    text = f.read()
  with open(os.path.join(prompts_dir, name + ".schema.json"), encoding="utf-8") as f:
    schema = json.load(f)
  return make(name, text, schema)


def register(conn, prompt):
  """Remember the prompt's content under its hash (idempotent). Returns the hash."""
  conn.execute(
      "INSERT OR IGNORE INTO ai_prompts (hash, name, text, schema, created_at) VALUES (?,?,?,?,?)",
      (prompt.hash, prompt.name, prompt.text, canonical_schema(prompt.schema),
       datetime.datetime.now().isoformat(timespec="seconds")))
  conn.commit()
  return prompt.hash


def get(conn, prompt_hash):
  row = conn.execute("SELECT * FROM ai_prompts WHERE hash = ?", (prompt_hash,)).fetchone()
  if row is None:
    return None
  return Prompt(row["name"], row["text"], json.loads(row["schema"]), row["hash"])
