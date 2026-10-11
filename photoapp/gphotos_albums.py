"""The albums this app has uploaded to, remembered in the database (ticket 232).

Google Photos only lists an app's own albums, ~10 per request (a 10-album listing is ~12 requests,
~15 s), and the user does not want the app talking to Google outside an export. So the export dialog
reads this table, and Google is contacted for the albums only on an explicit "Refresh from Google"
(`sync`), which also picks up albums made before the table existed and drops ones deleted on Google.
Every upload job records the album it used (`record_upload`), so the table fills itself.
"""

import json
import time


def list_all(conn):
  """[{"id", "title", "count"}] of every remembered album (the shape gphotos_rank.rank takes)."""
  return [{"id": r["id"], "title": r["title"], "count": r["item_count"]}
          for r in conn.execute("SELECT id, title, item_count FROM gphotos_albums")]


def synced_at(conn):
  """When the table was last synced with Google (epoch seconds), or None if never."""
  row = conn.execute("SELECT MAX(synced_at) FROM gphotos_albums").fetchone()
  return row[0]


def record_upload(conn, album_id, title, added, now=None):
  """Remember that `added` items went into album_id. A new album gets `title`; an existing one keeps
  the title it has (it may have been renamed on Google, which only a sync learns about)."""
  now = time.time() if now is None else now
  conn.execute(
      "INSERT INTO gphotos_albums (id, title, item_count, last_used) VALUES (?, ?, ?, ?) "
      "ON CONFLICT(id) DO UPDATE SET item_count = item_count + excluded.item_count, "
      "last_used = excluded.last_used, "
      "title = CASE WHEN title = '' THEN excluded.title ELSE title END",
      (album_id, title or "", added, now))
  conn.commit()


def sync(conn, albums, now=None):
  """Replace the table's contents with Google's own list (albums: Client.list_albums() shape): update
  titles and counts, add albums this app did not know about, forget ones Google no longer has."""
  now = time.time() if now is None else now
  keep = {a["id"] for a in albums}
  for row in conn.execute("SELECT id FROM gphotos_albums").fetchall():
    if row["id"] not in keep:
      conn.execute("DELETE FROM gphotos_albums WHERE id = ?", (row["id"],))
  for a in albums:
    conn.execute(
        "INSERT INTO gphotos_albums (id, title, item_count, synced_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET title = excluded.title, item_count = excluded.item_count, "
        "synced_at = excluded.synced_at",
        (a["id"], a["title"], a["count"], now))
  conn.commit()


def backfill_from_jobs(conn):
  """Remember albums that earlier upload jobs used (their rows still hold the album id and title),
  without contacting Google. Counts are the items that job sent. Returns how many were added."""
  added = 0
  for job in conn.execute("SELECT options FROM jobs WHERE kind = 'gphotos_upload' "
                          "AND options IS NOT NULL ORDER BY id").fetchall():
    try:
      options = json.loads(job["options"])
    except ValueError:
      continue
    album_id = options.get("album_id")
    if not album_id or conn.execute(
        "SELECT 1 FROM gphotos_albums WHERE id = ?", (album_id,)).fetchone():
      continue
    sent = max(0, len(options.get("items", [])) - len(options.get("failed", [])))
    record_upload(conn, album_id, options.get("album_title", ""), sent, now=0.0)
    added += 1
  return added
