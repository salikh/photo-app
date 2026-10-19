"""The 'video_stabilize' job (ticket 249, epic 246): make a stabilized tuning of a video.

The result is `<stem>.stabilized-<level>.mp4` next to the original, imported into the library,
described by `files.tuning_note` and linked (manual link, role 'tuning') to the Photo of the
original. The same level again overwrites its file; another level makes another file.
"""

import json
import os

from absl import logging

from photoapp import fileinfo
from photoapp import grouping
from photoapp import manual_links
from photoapp import paths
from photoapp import scan
from photoapp import thumb_populate
from photoapp import thumbs
from photoapp import video_stabilize


def source_for(conn, photo_id):
  """The live, non-derived video file row a stabilization of this Photo starts from: its original,
  else the first live video that is not itself a stabilized file. None if there is none."""
  rows = conn.execute(
      "SELECT f.id, f.path, f.duration, (f.id = p.original_file_id) AS is_original FROM files f "
      "JOIN photos p ON p.id = f.photo_id WHERE f.photo_id = ? AND f.missing = 0 "
      "ORDER BY is_original DESC, f.path", (photo_id,)).fetchall()
  for r in rows:
    if fileinfo.is_video(r["path"]) and not video_stabilize.is_stabilized_name(r["path"]):
      return r
  return None


def run(conn, settings, job, tools, set_note=None):
  """Run one stabilize job (job['file_id'] = the source video, job['options'] = the options dict).
  Returns the new file's id."""
  options = json.loads(job["options"]) if job["options"] else {}
  src = conn.execute("SELECT id, path, duration, missing FROM files WHERE id = ?",
                     (job["file_id"],)).fetchone()
  if src is None or src["missing"]:
    raise RuntimeError("file is gone")
  level = video_stabilize.resolve(options)["level"]
  dest_rel = video_stabilize.output_name(src["path"], level)
  src_abs = os.path.join(settings.pictures_dir, src["path"])
  dest_abs = os.path.join(settings.pictures_dir, dest_rel)

  note = video_stabilize.stabilize(tools, src_abs, dest_abs, options, duration=src["duration"],
                                   progress=set_note)
  # Not an existing row's old rendition: drop thumbnails of a file this run just replaced.
  old = conn.execute("SELECT id FROM files WHERE path = ?", (dest_rel,)).fetchone()
  if old is not None:
    thumbs.clear(settings.thumbs_dir, conn, old["id"], dest_rel)
  file_id = scan.import_single_file(conn, settings.pictures_dir, dest_rel)
  conn.execute("UPDATE files SET tuning_note = ? WHERE id = ?", (note, file_id))
  conn.commit()
  grouping.regroup(conn, {paths.dirname(dest_rel)})
  row = conn.execute("SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()
  source_photo = conn.execute("SELECT photo_id FROM files WHERE id = ?", (src["id"],)).fetchone()
  if row["photo_id"] != source_photo["photo_id"]:
    manual_links.link(conn, settings.state_dir, dest_rel, src["path"], "tuning")
  try:     # still + animated thumbnails now, so choosing this tuning shows a real tile at once
    thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, file_id, dest_rel)
  except Exception:      # noqa: BLE001 -- the tuning itself is done; thumbnails fill in lazily
    logging.exception("stabilize: thumbnails of %s", dest_rel)
  logging.info("stabilize %s -> %s (%s)", src["path"], dest_rel, note)
  return file_id
