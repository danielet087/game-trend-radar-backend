from __future__ import annotations

import argparse
import logging

from collectors.steam_upcoming import SteamUpcomingCollector, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect upcoming Steam games and keep only games above the follower threshold."
    )
    parser.add_argument("--output", default="output/steam_upcoming.json")
    parser.add_argument("--country", default="TW")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--min-followers", type=int, default=5000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--request-interval", type=float, default=2.0)
    parser.add_argument("--search-interval", type=float, default=10.0)
    parser.add_argument("--max-pages", type=int, default=100)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    collector = SteamUpcomingCollector(
        country=args.country,
        horizon_days=args.days,
        min_followers=args.min_followers,
        follower_workers=args.workers,
        follower_request_interval=args.request_interval,
        search_request_interval=args.search_interval,
        max_pages=args.max_pages,
    )
    payload = collector.collect()
    path = write_json(payload, args.output)
    print(f"Steam collection complete: {payload['count']} games -> {path}")


if __name__ == "__main__":
    main()
