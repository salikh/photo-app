# Video tunings: stabilization (epic 246)

Code: `photoapp/video_stabilize.py` (ffmpeg), `photoapp/stabilize_job.py` (job), `photoapp/static/stabilize.js`
(modal). Tickets 246-252.

## A tuning is a real file

A stabilized video is a file `<stem>.stabilized-<level>.mp4` in the original's directory, indexed like
any file and attached to the original's Photo through a **manual link** (role `tuning`), not by extending the
automatic grouping rule. Reason: the rule is stem-based and would need a version bump and a naming
convention nothing else shares; manual links are already durable (`manual_links.jsonl`), survive rescans and
moves, and `unlink` already detaches one. The cost: the name infix is only a convention, the link is what
matters. A re-run of the same level overwrites the file (atomically: `.part` then rename) and does not add
another link decision; another level is another file.

## Which tuning is shown

The Photo's `representative_file_id` — the same switch RAW+JPEG photos use ("Show this" in the Files pane).
Thumbnails, the animated hover preview, the loupe still and the `<video>` source all follow it. Deleting a
file makes the server pick a live representative (`grouping.fix_representatives` now also replaces a
representative that is *missing* when another member is live, at delete time and on every rescan). The
unstabilized original may be deleted: it stays in the Photo as a missing `original`, exactly like a deleted
DNG next to its JPEG; there is no source left to stabilize again and the API says so.

## Why these ffmpeg settings

Two passes: `vidstabdetect` (shakiness 5-8, accuracy 15) into a temp `.trf`, then `vidstabtransform`
(smoothing 10 frames for weak, 30 for strong, one static `optzoom` so borders are hidden) with a mild
`unsharp` to compensate the interpolation blur. Output H.264 High / yuv420p / AAC 128k in MP4 with
`+faststart`: plays everywhere (Android, iOS, every browser), CRF 23 preset medium gives quality close to a
phone original at usually a smaller size. Even dimensions are forced (H.264 requirement). ffmpeg
autorotates, so the output carries no rotation flag. HDR/10-bit sources are converted to 8-bit SDR — a known
limitation. Every parameter is in the tables at the top of `video_stabilize.py`, and an options dict can
override any of them (validated against `PARAMS`), which is how the modal can grow fields.

## Description

`files.tuning_note` (one line, e.g. `stabilized (strong): vidstab shakiness=8 ... ; H.264 crf=23 ...`) is
shown under the file in the Files pane and is also written into the MP4's `comment` tag. It is DB state: after
a rebuilt database the tag still holds it, but nothing reads it back yet (a possible follow-up).

## Scheduling

The medium queue (`app.state.medium_jobs`, see [concurrency-and-jobs.md](concurrency-and-jobs.md)) runs one
stabilization at a time at nice 10. Jobs are keyed by (source file, level), so double clicks dedupe. A
restart puts a running job back in the queue (it starts over).
