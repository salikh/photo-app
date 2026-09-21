"""Check that photos of a real year can be shown: request their pictures through the app.

  .venv/bin/python tools/check_images.py --year=2021 --sample=60 [--sizes=Thumb,Medium,Huge] [--dngs=20]

Read-only on the library. Uses the real state database (a copy is safer) and a scratch thumbnail
directory, so nothing is written to /zoo/Thumbs. For a random sample of the year's Photos it
requests /img/<size>/<file id> the way the browser does (the representative file, plus a number
of DNG originals that have no JPG) and reports every answer that is not an image, with the file
name, so "no missing images" is a measured fact.
"""

import argparse
import collections
import dataclasses
import io
import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from photoapp import api  # noqa: E402
from photoapp import config  # noqa: E402
from photoapp import db  # noqa: E402


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--year", required=True)
  ap.add_argument("--sample", type=int, default=60)
  ap.add_argument("--dngs", type=int, default=15, help="extra samples: RAW originals of Photos without a camera JPG")
  ap.add_argument("--raw_originals", type=int, default=0,
                  help="extra samples: the DNG original of paired Photos (what the G key shows)")
  ap.add_argument("--sizes", default="Thumb,Medium,Huge")
  ap.add_argument("--state_dir", default=os.path.expanduser("~/.local/share/photos"))
  ap.add_argument("--pictures_dir", default="/zoo/Pictures")
  ap.add_argument("--seed", type=int, default=1)
  args = ap.parse_args()

  scratch = tempfile.mkdtemp(prefix="check_images_")
  settings = config.Settings(pictures_dir=args.pictures_dir, thumbs_dir=os.path.join(scratch, "thumbs"),
                             state_dir=scratch, xmp_dry_run=True)
  # a private copy of the database: the app may migrate or write to it
  src = os.path.join(args.state_dir, "app.sqlite")
  dst = os.path.join(scratch, "app.sqlite")
  a, b = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(dst)
  a.backup(b)
  a.close()
  b.close()
  conn = db.open_state(scratch)
  client = TestClient(api.create_app(conn, settings))

  rnd = random.Random(args.seed)
  lo, hi = args.year + "/", args.year + "0"
  reps = conn.execute(
      "SELECT rf.id, rf.path FROM photos p JOIN files rf ON rf.id = p.representative_file_id "
      "WHERE rf.path >= ? AND rf.path < ? AND rf.missing = 0", (lo, hi)).fetchall()
  raw_only = conn.execute(
      "SELECT rf.id, rf.path FROM photos p JOIN files rf ON rf.id = p.representative_file_id "
      "WHERE rf.path >= ? AND rf.path < ? AND rf.missing = 0 AND lower(rf.path) LIKE '%.dng' "
      "AND NOT EXISTS (SELECT 1 FROM files c WHERE c.photo_id = p.id AND c.role = 'camera' AND c.missing = 0)",
      (lo, hi)).fetchall()
  raw_orig = conn.execute(
      "SELECT f.id, f.path FROM files f WHERE f.path >= ? AND f.path < ? AND f.missing = 0 AND "
      "f.role = 'original' AND lower(f.path) LIKE '%.dng'", (lo, hi)).fetchall()
  sample = (rnd.sample(reps, min(args.sample, len(reps))) + rnd.sample(raw_only, min(args.dngs, len(raw_only)))
            + rnd.sample(raw_orig, min(args.raw_originals, len(raw_orig))))
  print(f"{args.year}: {len(reps)} photos ({len(raw_only)} RAW without a JPG, {len(raw_orig)} DNG originals); "
        f"checking {len(sample)}", flush=True)

  sizes = args.sizes.split(",")
  failures, ok, times = [], collections.Counter(), collections.defaultdict(list)
  start = time.time()
  for n, (fid, path) in enumerate(sample, 1):
    for size in sizes:
      t = time.time()
      r = client.get(f"/img/{size}/{fid}")
      times[size].append(time.time() - t)
      good = False
      if r.status_code == 200:
        try:
          Image.open(io.BytesIO(r.content)).verify()
          good = True
        except Exception as e:
          failures.append((size, path, f"200 but not an image: {e}"))
      else:
        failures.append((size, path, f"HTTP {r.status_code}: {r.text[:80]}"))
      if good:
        ok[size] += 1
    if n % 10 == 0:
      print(f"  {n}/{len(sample)} done, {len(failures)} failures, {time.time() - start:.0f} s", flush=True)
  print(f"\nok per size: {dict(ok)} of {len(sample)}")
  for size in sizes:
    ts = sorted(times[size])
    print(f"{size:7s} median {ts[len(ts) // 2] * 1000:6.0f} ms   slowest {ts[-1] * 1000:6.0f} ms")
  print(f"failures: {len(failures)}")
  for f in failures[:40]:
    print("  ", f)
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
