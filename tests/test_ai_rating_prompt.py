import re

from photoapp import ai_prompts


def test_rating_prompt_names_every_schema_key_with_its_range():
  p = ai_prompts.load("rating")
  props = p.schema["properties"]
  assert set(p.schema["required"]) == set(props) and len(props) >= 8
  for key, spec in props.items():
    assert spec["type"] == "INTEGER"
    m = re.search(rf"^- {key} \((-?\d+) (?:to|or) (-?\d+)\)", p.text, re.M)
    assert m, f"prompt does not describe {key}"
    assert (int(m.group(1)), int(m.group(2))) == (spec["minimum"], spec["maximum"])
