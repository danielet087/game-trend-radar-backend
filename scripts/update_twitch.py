from __future__ import annotations

import argparse
import logging
import os

from collectors.twitch_live import collect_twitch, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect hourly Twitch game live metrics.")
    parser.add_argument("--output", default="output/twitch_live.json")
    parser.add_argument("--global-pages", type=int, default=10)
    parser.add_argument("--tracked-pages", type=int, default=50)
    parser.add_argument("--top-games", type=int, default=100)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")

    payload = collect_twitch(
        client_id=client_id,
        client_secret=client_secret,
        global_pages=args.global_pages,
        tracked_pages=args.tracked_pages,
        top_games_limit=args.top_games,
    )
    path = write_json(payload, args.output)
    print(
        f"Twitch collection complete: "
        f"{len(payload['top_games'])} top games, "
        f"{len(payload['tracked_games'])} tracked games -> {path}"
    )


if __name__ == "__main__":
    main()
