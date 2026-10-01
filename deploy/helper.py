"""Pure helpers for deploy.sh (kept in Python so they are testable).

  helper.py mounts CONFIG.toml    -> a RequiresMountsFor= line (or nothing)
  helper.py env ENV_FILE          -> systemd EnvironmentFile text from `export K="v"` lines
  helper.py listen CONFIG ARGS    -> "HOST PORT" the service will listen on
  helper.py render TEMPLATE K=V.. -> template with @K@ replaced
"""
import os
import re
import shlex
import sys
import tomllib


def mounts(config_path):
  try:
    with open(config_path, "rb") as f:
      cfg = tomllib.load(f)
  except OSError:
    return ""
  paths = [os.path.expanduser(cfg[k]) for k in ("pictures_dir", "thumbs_dir") if cfg.get(k)]
  paths = [p for p in paths if os.path.isabs(p)]
  return "RequiresMountsFor=" + " ".join(paths) if paths else ""


def env_text(text):
  out = []
  for line in text.splitlines():
    m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
    if not m:
      continue
    parts = shlex.split(m.group(2))
    value = parts[0] if parts else ""
    if value.startswith("<") and value.endswith(">"):  # untouched ENV.template placeholder
      continue
    out.append(f"{m.group(1)}={value}")
  return "\n".join(out) + ("\n" if out else "")


def listen(config_path, args_text):
  host, port = "0.0.0.0", 8080
  try:
    with open(config_path, "rb") as f:
      cfg = tomllib.load(f)
    host, port = cfg.get("host", host), int(cfg.get("port", port))
  except OSError:
    pass
  toks = shlex.split(args_text.split("=", 1)[1] if args_text.startswith("PHOTOS_ARGS=") else args_text)
  i = 0
  while i < len(toks):
    t = toks[i].lstrip("-")
    if t.startswith(("host=", "port=")):
      k, v = t.split("=", 1)
    elif t in ("host", "port") and i + 1 < len(toks):
      k, v = t, toks[i + 1]
      i += 1
    else:
      i += 1
      continue
    if k == "host":
      host = v
    else:
      port = int(v)
    i += 1
  return host, port


def render(template, values):
  for k, v in values.items():
    template = template.replace(f"@{k}@", v)
  left = re.findall(r"@[A-Z]+@", template)
  if left:
    raise SystemExit(f"unreplaced placeholders: {left}")
  return template


def main(argv):
  cmd = argv[1]
  if cmd == "mounts":
    print(mounts(argv[2]))
  elif cmd == "env":
    sys.stdout.write(env_text(open(argv[2]).read()))
  elif cmd == "listen":
    text = open(argv[3]).read().strip() if os.path.exists(argv[3]) else ""
    h, p = listen(argv[2], text)
    print(h, p)
  elif cmd == "render":
    sys.stdout.write(render(open(argv[2]).read(), dict(a.split("=", 1) for a in argv[3:])))
  else:
    raise SystemExit(__doc__)


if __name__ == "__main__":
  main(sys.argv)
