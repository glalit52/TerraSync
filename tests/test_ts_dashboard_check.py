"""The console consistency check, which is what CI runs in place of a rebuild."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(dash_dir: Path):
    """Import the checker with its DASH directory pointed at a fixture."""
    spec = importlib.util.spec_from_file_location(
        "ts_check", ROOT / "scripts" / "check_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DASH = dash_dir
    return module


def _fixture(tmp_path: Path, *, embed_matching: bool = True,
             chip_on_disk: bool = True, orphan: bool = False) -> Path:
    dash = tmp_path / "terrashield"
    (dash / "chips").mkdir(parents=True)
    data = {
        "sites": [{
            "id": "AOI-1", "change_count": 3,
            "changes": [{"id": "c1"}],
            "chips": [{
                "change_id": "c1",
                "before": "chips/c1-before.png", "after": "chips/c1-after.png",
                "mask": "chips/c1-mask.png",
                "detail_before": "chips/c1-db.png",
                "detail_after": "chips/c1-da.png",
                "detail_mask": "chips/c1-dm.png",
            }],
        }],
    }
    (dash / "data.json").write_text(json.dumps(data))
    (dash / "template.html").write_text("<p>__DATA__</p>")
    if chip_on_disk:
        for name in ("before", "after", "mask", "db", "da", "dm"):
            (dash / "chips" / f"c1-{name}.png").write_bytes(b"\x89PNG")
    if orphan:
        (dash / "chips" / "stale.png").write_bytes(b"\x89PNG")
    embedded = data if embed_matching else {"sites": []}
    (dash / "index.html").write_text(
        '<script type="application/json" id="payload">'
        + json.dumps(embedded) + "</script>")
    return dash


def test_a_consistent_console_passes(tmp_path, capsys):
    module = _load(_fixture(tmp_path))
    assert module.main() == 0
    assert "consistent" in capsys.readouterr().out


def test_a_stale_page_fails(tmp_path, capsys):
    """The drift that actually happens: rebuild the data, forget the page."""
    module = _load(_fixture(tmp_path, embed_matching=False))
    assert module.main() == 1
    assert "differs from data.json" in capsys.readouterr().err


def test_a_missing_chip_fails(tmp_path, capsys):
    module = _load(_fixture(tmp_path, chip_on_disk=False))
    assert module.main() == 1
    assert "referenced but not committed" in capsys.readouterr().err


def test_an_orphan_chip_fails(tmp_path, capsys):
    module = _load(_fixture(tmp_path, orphan=True))
    assert module.main() == 1
    assert "nothing references it" in capsys.readouterr().err


def test_an_unbuilt_console_is_not_an_error(tmp_path, capsys):
    """Before the first build there is nothing to be inconsistent with."""
    empty = tmp_path / "terrashield"
    empty.mkdir()
    module = _load(empty)
    assert module.main() == 0
    assert "nothing to check" in capsys.readouterr().out


def test_the_real_console_is_consistent():
    """Runs against what is actually committed, exactly as CI does."""
    module = _load(ROOT / "dashboard" / "terrashield")
    assert module.main() == 0
