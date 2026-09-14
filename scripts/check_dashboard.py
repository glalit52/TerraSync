#!/usr/bin/env python3
"""Check the committed TerraShield console against its own dataset.

Not the same check `check_dashboard_fresh.py` does for the MRV dashboard. That
one regenerates the dataset and compares, which it can afford because the feed
is a fast deterministic simulation. Regenerating this one means running six
months of the monitoring pipeline over four sites -- hours of work -- so a CI
job cannot do it, and pretending otherwise would give a check that is either
skipped or always red.

What this does check is the drift that actually happens: someone rebuilds
data.json and forgets to rebuild index.html, or prunes chips, or adds a site
whose imagery never got rendered. All of that is cheap to catch and all of it
produces a page that is wrong in a way a reader cannot see.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"

PAYLOAD = re.compile(
    r'<script type="application/json" id="payload">(.*?)</script>', re.S)


def main() -> int:
    problems: list[str] = []

    for name in ("data.json", "index.html", "template.html"):
        if not (DASH / name).exists():
            print(f"no {name} committed; nothing to check")
            return 0

    data = json.loads((DASH / "data.json").read_text())
    page = (DASH / "index.html").read_text()

    match = PAYLOAD.search(page)
    if not match:
        problems.append("index.html has no payload script; rebuild it with "
                        "scripts/build_dashboard.py")
    else:
        embedded = json.loads(match.group(1).replace("<\\/script", "</script"))
        if embedded != data:
            problems.append(
                "the data embedded in index.html differs from data.json; "
                "re-run scripts/build_dashboard.py")

    if "__DATA__" in page:
        problems.append("index.html still contains the __DATA__ placeholder")

    referenced: set[str] = set()
    for site in data.get("sites", []):
        for chip in site.get("chips", []):
            for key in ("before", "after", "mask", "detail_before",
                        "detail_after", "detail_mask"):
                ref = chip.get(key)
                if not ref:
                    problems.append(
                        f"{site['id']}: chip {chip.get('change_id')} has no "
                        f"{key} image")
                    continue
                referenced.add(ref)
                if not (DASH / ref).exists():
                    problems.append(f"{ref} is referenced but not committed")

    on_disk = {f"chips/{p.name}" for p in (DASH / "chips").glob("*.png")}
    for orphan in sorted(on_disk - referenced):
        problems.append(f"{orphan} is committed but nothing references it")

    #: A committed page whose findings do not match its own site rows is worse
    #: than no page: it reads as authoritative and is not.
    for site in data.get("sites", []):
        if site["change_count"] < len(site.get("changes", [])):
            problems.append(
                f"{site['id']}: change_count {site['change_count']} is below "
                f"the {len(site['changes'])} changes listed")

    if problems:
        print("TerraShield console is out of sync:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    sites = len(data.get("sites", []))
    print(f"TerraShield console is consistent: {sites} site(s), "
          f"{len(referenced)} imagery chip(s), payload matches data.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
