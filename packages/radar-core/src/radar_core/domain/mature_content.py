"""Reviewed ordinary games whose Steam descriptor 4 is not a porn verdict.

Reviews use the exact Steam AppID and its official appdetails content notes.
They only remove descriptor 4; a current Adult Only Sexual Content (3) flag
always wins. Unreviewed descriptor-4 games keep the existing review gate.
"""
from __future__ import annotations

REVIEWED_NON_EROTIC = {
    2532010: {
        "name": "Territorial Hissing",
        "reason": "Comedy action adventure; mature imagery is incidental to gameplay.",
    },
    4100630: {
        "name": "Pull Me In",
        "reason": "Supernatural life simulation; optional shower nudity establishes horror atmosphere.",
    },
    4310350: {
        "name": "Siren's Call: Second Wind",
        "reason": "Psychological mystery adventure; official notes explicitly state there is no nudity.",
    },
    4801860: {
        "name": "Thump: Tiny Heart Flutters",
        "reason": "Romance jigsaw game; current official descriptors are 1/5 and notes only mention revealing clothing.",
    },
}
for _appid, _review in REVIEWED_NON_EROTIC.items():
    _review.update(
        checked_at="2026-11-08",
        source_url=f"https://store.steampowered.com/api/appdetails?appids={_appid}&l=english&cc=us",
    )


def reviewed_descriptors(appid, descriptors: set) -> set:
    """Narrow an audited false positive without granting an adult-only exception."""
    try:
        reviewed = int(appid) in REVIEWED_NON_EROTIC
    except (ValueError, TypeError, OverflowError):
        reviewed = False
    if reviewed and 3 not in descriptors:
        return descriptors - {4}
    return descriptors
