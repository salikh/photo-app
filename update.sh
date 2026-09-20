#!/bin/bash

set -e
NICE=(nice -n 19 ionice -c3)

(echo -n "##################### STARTING hash_dir on /zoo/Thumbs "; date) >&2
${NICE[@]} ./hash_dir.py --db ~/thumbs.db --root_dir /zoo/Thumbs --dir /zoo/Thumbs --logtostderr --v=3
(echo -n "##################### COMPLETED hash_dir on /zoo/Thumbs "; date) >&2
(echo -n "##################### STARTING hash_dir on /zoo/Pictures "; date) >&2
${NICE[@]} ./hash_dir.py --db ~/zoo.db --root_dir /zoo/Pictures --dir /zoo/Pictures --logtostderr --v=3
(echo -n "##################### COMPLETED hash_dir on /zoo/Pictures"; date) >&2
(echo -n "##################### STARTING file_metadata on /zoo/Pictures "; date) >&2
${NICE[@]} ./file_metadata.py --logtostderr --v=3 --root_dir /zoo/Pictures --dir /zoo/Pictures --db ~/zoo-metadata.db --hashes_db ~/zoo.db
(echo -n "##################### COMPLETED file_metadata on /zoo/Pictures"; date) >&2
(echo -n "##################### STARTING file_metadata on /zoo/Thumbs "; date) >&2
${NICE[@]} ./file_metadata.py --logtostderr --v=3 --root_dir /zoo/Thumbs --dir /zoo/Thumbs --db ~/thumbs-metadata.db --hashes_db ~/thumbs.db
(echo -n "##################### COMPLETED file_metadata on /zoo/Thumbs "; date) >&2
