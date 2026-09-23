from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from opencc import OpenCC

LOG = logging.getLogger(__name__)
STORE_BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
TAIPEI = ZoneInfo("Asia/Taipei")
OPENCC = OpenCC("s2t")
HAN = re.compile(r"[\u3400-\u9fff]")
APP_TAG_RE = re.compile(
    r'<a[^>]*class=["\'][^"\']*\bapp_tag\b[^"\']*["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
ALLOWED_ASSET_HOSTS = ("steamstatic.com", "steamcdn-a.akamaihd.net")
LANG_IDS = {"english": 0, "schinese": 6, "tchinese": 7}
OTHER_LANGUAGE_NAMES = {
    1: "德文", 2: "法文", 3: "義大利文", 4: "韓文", 5: "西班牙文",
    8: "俄文", 9: "泰文", 10: "日文", 11: "葡萄牙文", 12: "波蘭文",
    13: "丹麥文", 14: "荷蘭文", 15: "芬蘭文", 16: "挪威文", 17: "瑞典文",
    18: "匈牙利文", 19: "捷克文", 20: "羅馬尼亞文", 21: "土耳其文",
    22: "巴西葡萄牙文", 23: "保加利亞文", 24: "希臘文", 25: "阿拉伯文",
    26: "烏克蘭文", 27: "拉丁美洲西班牙文", 28: "越南文",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def valid_date(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return datetime.fromisoformat(value).date().isoformat() == value
    except ValueError:
        return False


def steam_asset_url(appid: int, fmt: object, filename: object) -> str:
    if not isinstance(fmt, str) or not isinstance(filename, str):
        return ""
    if "${FILENAME}" not in fmt or not filename or ".." in filename.split("/"):
        return ""
    url = urljoin(ASSET_BASE, fmt.replace("${FILENAME}", filename))
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not any(
        host == x or host.endswith("." + x) for x in ALLOWED_ASSET_HOSTS
    ):
        return ""
    if f"/steam/apps/{appid}/" not in parsed.path or ".." in parsed.path.split("/"):
        return ""
    return url if parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else ""


def request_json(
    session: requests.Session, url: str, *, params: dict, attempts: int = 4
) -> dict:
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, timeout=35)
            if response.status_code == 429:
                time.sleep(12 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
        except (requests.RequestException, ValueError, TypeError) as exc:
            LOG.warning(
                "Steam JSON request retry %d/%d: %s",
                attempt + 1, attempts, type(exc).__name__,
            )
            if attempt + 1 < attempts:
                time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Steam JSON request failed after {attempts} attempts: {url}")


def browse_one(session: requests.Session, appid: int, language: str) -> dict:
    payload = {
        "ids": [{"appid": appid}],
        "context": {"country_code": "TW", "language": language, "steam_realm": 1},
        "data_request": {
            "include_basic_info": True,
            "include_release": True,
            "include_assets": True,
            "include_supported_languages": True,
        },
    }
    data = request_json(
        session,
        STORE_BROWSE,
        params={"input_json": json.dumps(payload, separators=(",", ":"))},
    )
    rows = (data.get("response") or {}).get("store_items") or []
    for row in rows:
        if isinstance(row, dict) and row.get("appid") == appid:
            return row
    raise RuntimeError(f"Steam Store Browse missing AppID {appid} in {language}")


def read_support(row: dict) -> dict:
    langs = row.get("supported_languages")
    if not isinstance(langs, list) or not langs:
        return {"tchinese": None, "schinese": None, "english": None}
    allowed = {
        x.get("elanguage")
        for x in langs
        if isinstance(x, dict)
        and type(x.get("elanguage")) is int
        and x.get("supported") is True
    }
    result = {
        "tchinese": LANG_IDS["tchinese"] in allowed,
        "schinese": LANG_IDS["schinese"] in allowed,
        "english": LANG_IDS["english"] in allowed,
    }
    if not any(result.values()):
        others = [
            OTHER_LANGUAGE_NAMES[x] for x in sorted(allowed)
            if x in OTHER_LANGUAGE_NAMES
        ]
        if others:
            result["other_languages"] = others
    return result


def official_chinese_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 240 and HAN.search(value) else None


def appdetails(session: requests.Session, appid: int) -> dict:
    data = request_json(
        session,
        APPDETAILS,
        params={"appids": appid, "cc": "TW", "l": "english"},
    )
    row = data.get(str(appid)) or {}
    if row.get("success") is not True or not isinstance(row.get("data"), dict):
        raise RuntimeError(f"Steam appdetails unavailable for {appid}")
    return row["data"]


def fetch_tags(session: requests.Session, appid: int) -> list[str]:
    try:
        response = session.get(
            STORE_PAGE.format(appid=appid),
            params={"cc": "TW", "l": "english"},
            timeout=35,
            cookies={"birthtime": "0", "lastagecheckage": "1-January-1990"},
        )
        if response.status_code == 429:
            return []
        response.raise_for_status()
    except requests.RequestException:
        return []
    values: list[str] = []
    for raw in APP_TAG_RE.findall(response.text):
        value = html.unescape(TAG_STRIP_RE.sub("", raw))
        value = re.sub(r"\s+", " ", value).strip()
        if not value or value == "+" or value in values:
            continue
        values.append(value)
        if len(values) >= 20:
            break
    return values


def build_record(
    session: requests.Session,
    *,
    appid: int,
    followers: int,
    event_release_date: str,
    follower_checked_at: str | None,
) -> dict:
    en = browse_one(session, appid, "english")
    time.sleep(1.0)
    tw = browse_one(session, appid, "tchinese")
    time.sleep(1.0)
    cn = browse_one(session, appid, "schinese")
    details = appdetails(session, appid)
    tags = fetch_tags(session, appid)

    name_en = str(
        en.get("name") or details.get("name") or f"Steam App {appid}"
    ).strip()
    name_tw = official_chinese_title(tw.get("name"))
    name_cn = official_chinese_title(cn.get("name"))

    release = en.get("release") or {}
    stamp = release.get("steam_release_date")
    release_time_utc = None
    release_start = event_release_date
    if type(stamp) in (int, float) or (
        isinstance(stamp, str) and stamp.isdigit()
    ):
        instant = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        release_time_utc = instant.isoformat().replace("+00:00", "Z")
        release_start = instant.astimezone(TAIPEI).date().isoformat()

    assets = en.get("assets") or {}
    if not isinstance(assets, dict):
        assets = {}
    fmt = assets.get("asset_url_format")
    header = (
        steam_asset_url(appid, fmt, assets.get("header"))
        or str(details.get("header_image") or "")
    )
    main = steam_asset_url(appid, fmt, assets.get("main_capsule"))
    capsule = (
        steam_asset_url(appid, fmt, assets.get("small_capsule"))
        or main or header
    )

    genres = []
    for item in details.get("genres") or []:
        if isinstance(item, dict) and isinstance(item.get("description"), str):
            value = item["description"].strip()
            if value and value not in genres:
                genres.append(value)

    record = {
        "appid": appid,
        "name": name_en,
        "name_en": name_en,
        "name_zh_tw": name_tw,
        "name_zh_cn": name_cn,
        "name_zh_tw_traditional": OPENCC.convert(name_tw) if name_tw else None,
        "name_zh_cn_traditional": OPENCC.convert(name_cn) if name_cn else None,
        "name_en_traditional": name_en,
        "language_support": read_support(en),
        "release_raw": release_start,
        "release_start": release_start,
        "release_end": release_start,
        "release_precision": "day",
        "release_display_precision": "date_full",
        "release_date_timezone": "Asia/Taipei",
        "release_date_basis": "steam_store_browse_release_time",
        "release_time_utc": release_time_utc,
        "release_time_source": STORE_BROWSE,
        "followers": followers,
        "follower_checked_at": follower_checked_at or utc_now(),
        "follower_source": "Steam Community XML memberCount",
        "official_ge5000": True,
        "sexual_content_screened": True,
        "capsule_image": capsule,
        "header_image": header,
        "main_capsule_image": main,
        "store_url": STORE_PAGE.format(appid=appid),
        "short_description": str(details.get("short_description") or "").strip(),
        "genres": genres,
        "tags": tags,
        "content_enriched_at": utc_now(),
        "content_enrichment_source": (
            "Steam Store Browse + appdetails + public Store tags"
        ),
        "content_enrichment_version": 1,
    }
    if not record["header_image"] and not record["main_capsule_image"]:
        raise RuntimeError(f"Steam did not return usable artwork for {appid}")
    if not isinstance(record["language_support"], dict):
        raise RuntimeError(f"Steam did not return language support for {appid}")
    return record


def upsert_document(path: Path, record: dict, event_release_date: str) -> bool:
    doc = json.loads(path.read_text(encoding="utf-8"))
    games = doc.get("games")
    if not isinstance(games, list):
        raise RuntimeError(f"Invalid games array in {path}")
    appid = int(record["appid"])
    existing = next(
        (g for g in games if int(g.get("appid", -1)) == appid), None
    )
    signature = f"{appid}:{record['followers']}:{event_release_date}"
    if existing:
        already = (
            existing.get("content_enrichment_signature") == signature
            and existing.get("header_image")
            and isinstance(existing.get("language_support"), dict)
            and isinstance(existing.get("tags"), list)
            and int(existing.get("followers", 0)) >= 5000
        )
        if already:
            return False
        existing.update({
            k: v for k, v in record.items() if v is not None and v != ""
        })
        existing["content_enrichment_signature"] = signature
    else:
        row = dict(record)
        row["content_enrichment_signature"] = signature
        games.append(row)
    games.sort(key=lambda g: (
        str(g.get("release_start") or "9999-12-31"),
        int(g.get("appid", 0)),
    ))
    doc["games"] = games
    doc["count"] = len(games)
    doc["generated_at"] = utc_now()
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--appid", type=int, required=True)
    parser.add_argument("--followers", type=int, required=True)
    parser.add_argument("--release-date", required=True)
    parser.add_argument("--follower-checked-at")
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.appid <= 0:
        raise SystemExit("appid must be positive")
    if args.followers < 5000:
        raise SystemExit(
            "content backend only accepts official Followers >= 5000"
        )
    if not valid_date(args.release_date):
        raise SystemExit("release-date must be an exact YYYY-MM-DD")
    target = args.data_dir / "steam_upcoming.json"
    if not target.exists():
        raise SystemExit(f"Missing frontend file: {target}")

    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarContentBackend/1.0"
    record = build_record(
        session,
        appid=args.appid,
        followers=args.followers,
        event_release_date=args.release_date,
        follower_checked_at=args.follower_checked_at,
    )
    changed = upsert_document(target, record, args.release_date)
    print(
        "CONTENT_ENRICHMENT_RESULT",
        json.dumps({
            "appid": args.appid,
            "followers": args.followers,
            "release_date": record["release_start"],
            "tags": len(record["tags"]),
            "genres": len(record["genres"]),
            "header": bool(record["header_image"]),
            "main_capsule": bool(record["main_capsule_image"]),
            "changed": changed,
        }, ensure_ascii=False),
        flush=True,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    main()
