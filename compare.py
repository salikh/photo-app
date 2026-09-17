#!/usr/bin/env python3
"""Compare the 'hashes' tables of two sqlite3 databases.

Reports:
  - hashes present only in the first database, and only in the second
  - duplication statistics per database (hashes with more than one filename)

Usage:
  compare.py DB1 DB2
"""

import argparse
import sqlite3
import sys
from collections import defaultdict


def load_hashes(db_path):
    """Return dict: hash -> list of filenames, from the 'hashes' table."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute("SELECT hash, filename FROM hashes")
        hash_to_files = defaultdict(list)
        for hash_, filename in cur:
            hash_to_files[hash_].append(filename)
        return hash_to_files
    finally:
        conn.close()


def print_duplication_stats(label, hash_to_files):
    total_files = sum(len(files) for files in hash_to_files.values())
    total_hashes = len(hash_to_files)
    dupes = {h: files for h, files in hash_to_files.items() if len(files) > 1}
    dup_extra_copies = sum(len(files) - 1 for files in dupes.values())

    print(f"--- Duplication stats for {label} ---")
    print(f"  total files:            {total_files}")
    print(f"  distinct hashes:        {total_hashes}")
    print(f"  hashes with duplicates: {len(dupes)}")
    print(f"  redundant copies:       {dup_extra_copies}")
    print()

    if dupes:
        print(f"  hashes with the most copies:")
        for h, files in sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:10]:
            print(f"    {h}  ({len(files)} copies)")
            for f in files:
                print(f"      {f}")
    print()


def print_unique_hashes(label_a, label_b, only_in_a):
    print(f"--- Hashes only in {label_a} (not in {label_b}): {len(only_in_a)} ---")
    for h, files in sorted(only_in_a.items()):
        for f in files:
            print(f"  {h}  {f}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Compare 'hashes' tables of two sqlite3 databases.")
    parser.add_argument("db1", help="path to first sqlite3 database")
    parser.add_argument("db2", help="path to second sqlite3 database")
    args = parser.parse_args()

    hashes1 = load_hashes(args.db1)
    hashes2 = load_hashes(args.db2)

    set1 = set(hashes1)
    set2 = set(hashes2)

    only_in_1 = {h: hashes1[h] for h in (set1 - set2)}
    only_in_2 = {h: hashes2[h] for h in (set2 - set1)}
    common = set1 & set2

    print(f"Database A: {args.db1}")
    print(f"Database B: {args.db2}")
    print()
    print(f"Distinct hashes in A: {len(set1)}")
    print(f"Distinct hashes in B: {len(set2)}")
    print(f"Common hashes:        {len(common)}")
    print(f"Only in A:            {len(only_in_1)}")
    print(f"Only in B:            {len(only_in_2)}")
    print()

    print_unique_hashes(args.db1, args.db2, only_in_1)
    print_unique_hashes(args.db2, args.db1, only_in_2)

    print_duplication_stats(args.db1, hashes1)
    print_duplication_stats(args.db2, hashes2)


if __name__ == "__main__":
    sys.exit(main())
