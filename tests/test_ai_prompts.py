from photoapp import ai_prompts

SCHEMA = {"type": "OBJECT", "properties": {"a": {"type": "INTEGER"}}}


def test_hash_is_content_based():
  a = ai_prompts.make("p", "rate it", SCHEMA)
  assert a.hash == ai_prompts.make("other-name", "rate it", dict(SCHEMA)).hash   # name is not content
  assert a.hash != ai_prompts.make("p", "rate it!", SCHEMA).hash
  assert a.hash != ai_prompts.make("p", "rate it", {**SCHEMA, "required": ["a"]}).hash
  reordered = {"properties": {"a": {"type": "INTEGER"}}, "type": "OBJECT"}
  assert a.hash == ai_prompts.make("p", "rate it", reordered).hash               # key order ignored


def test_register_is_idempotent_and_get_returns_the_text(conn):
  p = ai_prompts.make("p", "rate it", SCHEMA)
  assert ai_prompts.get(conn, p.hash) is None
  ai_prompts.register(conn, p)
  ai_prompts.register(conn, p)
  assert conn.execute("SELECT COUNT(*) FROM ai_prompts").fetchone()[0] == 1
  assert ai_prompts.get(conn, p.hash) == p


def test_load_reads_the_files(tmp_path):
  (tmp_path / "x.md").write_text("hello")
  (tmp_path / "x.schema.json").write_text('{"type": "OBJECT"}')
  p = ai_prompts.load("x", str(tmp_path))
  assert p.text == "hello" and p.schema == {"type": "OBJECT"}
  (tmp_path / "x.md").write_text("hello2")
  assert ai_prompts.load("x", str(tmp_path)).hash != p.hash
