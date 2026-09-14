#!/usr/bin/env python3
"""Inline the dataset into the TerraShield console template.

The data ships inside the page rather than being fetched, so the console opens
straight from the filesystem with no server and cannot render half-empty.
Imagery chips stay as separate files beside it -- base64 in the page would
triple their size for no benefit, and an <img> works over file:// where fetch
does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"


def main() -> int:
    data = (DASH / "data.json").read_text().strip()
    template = (DASH / "template.html").read_text()
    if "__DATA__" not in template:
        print("template has no __DATA__ placeholder", file=sys.stderr)
        return 1
    #: The payload sits in a <script type="application/json">, so the only
    #: sequence that can break out of it is a literal closing script tag.
    safe = data.replace("</script", "<\\/script")
    (DASH / "index.html").write_text(template.replace("__DATA__", safe))
    chips = len(list((DASH / "chips").glob("*.png")))
    size = (DASH / "index.html").stat().st_size
    print(f"wrote {DASH / 'index.html'} ({size / 1024:.0f} KB, {chips} chips)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
