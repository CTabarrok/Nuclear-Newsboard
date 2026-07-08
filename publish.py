#!/usr/bin/env python3
"""Promote pending.json to the live archive: news-<date>.json + archive_index.json.
Run by the Publish workflow after email approval (or locally)."""

import json
import os
import re
import sys
from pathlib import Path

pending = Path("pending.json")
if not pending.exists():
    sys.exit("No pending.json to publish.")

data = json.loads(pending.read_text())
date = data["date"]
items = data.get("items", [])
if not items:
    sys.exit("pending.json has no items — refusing to publish an empty board.")

# PICKS: story numbers from the approval email, e.g. "1,4,6" (1-based).
raw = os.environ.get("PICKS", "").strip()
nums = [int(n) for n in re.findall(r"\d+", raw)] or [1, 2, 3]
bad = [n for n in nums if n < 1 or n > len(items)]
if bad:
    sys.exit(f"Picks {bad} out of range — pending.json has {len(items)} stories.")
if len(nums) != len(set(nums)):
    sys.exit(f"Duplicate picks in '{raw}'.")
if len(nums) != 3:
    sys.exit(f"Expected exactly 3 picks, got {len(nums)}: {nums}")

data["items"] = [items[n - 1] for n in nums]
if nums != [1, 2, 3]:
    data["trend_note"] = None  # note was written for the default set
print(f"Publishing stories {nums} of {len(items)} candidates.")

Path(f"news-{date}.json").write_text(json.dumps(data, indent=2))

idx_path = Path("archive_index.json")
index = json.loads(idx_path.read_text()) if idx_path.exists() else {"dates": []}
if date not in index["dates"]:
    index["dates"].append(date)
index["dates"] = sorted(set(index["dates"]), reverse=True)
idx_path.write_text(json.dumps(index, indent=2))

print(f"Published news-{date}.json ({len(data['items'])} items). "
      f"Archive now spans {len(index['dates'])} editions.")
