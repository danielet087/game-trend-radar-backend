"""One-time add Traditional-script display titles to the preserved eligible subset.

No requests to Steam, Followers, Steam Community or any third party.
The 11,467-game original discovery catalog and saved cursors are never touched.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from scripts.steam_localized_titles import add_traditional_display_names

LOG = logging.getLogger(__name__)


def run(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get("games")
    if (not isinstance(rows, list) or doc.get("count") != len(rows)
            or doc.get("source_count") != 11467
            or not 2000 <= len(rows) <= 6000):
        raise RuntimeError("Unexpected exact-date eligible snapshot; no changes made")
    converted_cn = converted_tw = 0
    for row in rows:
        original_tw = row.get("name_zh_tw")
        original_cn = row.get("name_zh_cn")
        add_traditional_display_names(row)
        converted_tw += bool(
            original_tw and row.get("name_zh_tw_traditional") != original_tw
        )
        converted_cn += bool(
            original_cn and row.get("name_zh_cn_traditional") != original_cn
        )
    results = {
        "eligible": len(rows),
        "tw_script_converted": converted_tw,
        "cn_script_converted": converted_cn,
    }
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    LOG.info("ELIGIBLE_TRADITIONAL_SCRIPT %s", results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path,
                        default=Path("data/steam_candidates_eligible.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    print("ELIGIBLE_TRADITIONAL_SCRIPT", json.dumps(run(args.snapshot)))
