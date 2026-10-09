"""Official Store title selection and display rules without runtime I/O."""
from __future__ import annotations

import re
from typing import Callable


HAN = re.compile(r"[\u3400-\u9fff]")


def actual_zh_tw_title(value: object, *, han=HAN) -> str | None:
    """Accept only a usable Chinese title actually returned by the Store."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or not han.search(name) or len(name) > 240:
        return None
    return name


def display_in_traditional(raw: object, *, convert: Callable) -> str | None:
    """Clean a display value and delegate script conversion to the caller."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return convert(raw.strip())


def add_traditional_display_names(
    game: dict, *, display: Callable, han=HAN,
) -> None:
    """Add display fields without changing raw Store names or language flags."""
    for source, target in (
        ("name_zh_tw", "name_zh_tw_traditional"),
        ("name_zh_cn", "name_zh_cn_traditional"),
        ("name_en", "name_en_traditional"),
    ):
        value = game.get(source)
        if isinstance(value, str) and han.search(value):
            game[target] = display(value)
        else:
            game.pop(target, None)

    game["display_name"] = (
        game.get("name_zh_tw_traditional")
        or game.get("name_zh_cn_traditional")
        or game.get("name_en")
        or game.get("name")
        or f"Steam App {game.get('appid')}"
    )
    game["display_name_source"] = (
        "tchinese" if game.get("name_zh_tw_traditional")
        else "schinese_converted" if game.get("name_zh_cn_traditional")
        else "english"
    )
