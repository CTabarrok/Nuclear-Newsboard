#!/usr/bin/env python3
"""Promote pending.json to the live archive: news-<date>.json + archive_index.json.
Run by the Publish workflow after email approval (or locally)."""

import json
import sys
from pathlib import Path

pending = Path("pending.json")
if not pending.exists():
    sys.exit("No pending.json to publish.")

data = json.loads(pending.read_text())
date = data["date"]
if len(data.get("items", [])) < 1:
    sys.exit("pending.json has no items — refusing to publish an empty board.")

Path(f"news-{date}.json").write_text(json.dumps(data, indent=2))

idx_path = Path("archive_index.json")
index = json.loads(idx_path.read_text()) if idx_path.exists() else {"dates": []}
if date not in index["dates"]:
    index["dates"].append(date)
index["dates"] = sorted(set(index["dates"]), reverse=True)
idx_path.write_text(json.dumps(index, indent=2))

print(f"Published news-{date}.json ({len(data['items'])} items). "
      f"Archive now spans {len(index['dates'])} editions.")
