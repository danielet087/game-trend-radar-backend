from __future__ import annotations

import argparse
import logging
import os

from collectors.youtube_live import collect_youtube, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect hourly YouTube gaming live metrics.")
    parser.add_argument("--output", default="output/youtube_live.json")
    parser.add_argument("--search-pages", type=int, default=2)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("YOUTUBE_API_KEY is required")

    payload = collect_youtube(api_key, search_pages=args.search_pages)
    path = write_json(payload, args.output)
    print(
        "YouTube collection complete: "
        f"streams={payload['coverage']['live_gaming_stream_sample_size']} "
        f"matched={payload['coverage']['matched_stream_count']} "
        f"games={len(payload['top_games'])} -> {path}"
    )


if __name__ == "__main__":
    main()
