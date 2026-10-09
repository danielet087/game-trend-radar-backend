"""Twitch queue receipt CLI using canonical intake and queue composition."""
from __future__ import annotations
import argparse
import os
import subprocess
from pathlib import Path
from radar_backend.adapters.twitch_intake import (
    collect, dispatch, read_json, write_json, apply_queue_batch,
    CHECKPOINT, MASTER, STATE,
)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', required=True, choices=['collect', 'apply', 'dispatch'])
    parser.add_argument('--frontend-path', type=Path)
    parser.add_argument('--frontend-commit')
    parser.add_argument('--batch', required=True, type=Path)
    parser.add_argument('--max-seconds', type=int, default=300)
    args = parser.parse_args()
    master, state, checkpoint = (read_json(MASTER), read_json(STATE, optional=True), read_json(CHECKPOINT))
    if args.phase == 'collect':
        if args.frontend_path is None or args.frontend_commit is None:
            parser.error('collect requires an immutable frontend snapshot')
        actual = subprocess.check_output(['git', '-C', str(args.frontend_path), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != args.frontend_commit:
            parser.error('frontend snapshot commit mismatch')
        paths = [Path('data/steam_followers_cache.json'), Path('data/steam_followers_checkpoint.json'), Path('experiments/steam_official_followers_20260922/checkpoint.json'), Path('experiments/steam_official_nearfirst_20260922/checkpoint.json')]
        caches = [checkpoint, *(read_json(p) for p in paths if p.is_file())]
        batch = collect(args.frontend_path, args.frontend_commit, master, state, caches=caches, max_seconds=args.max_seconds)
        write_json(args.batch, batch)
    elif args.phase == 'dispatch':
        batch = dispatch(master, state, token=os.environ.get('CONTENT_BACKEND_TOKEN', ''), target=os.environ.get('CONTENT_BACKEND_REPOSITORY', 'danielet087/game-trend-radar-content-backend'), max_seconds=args.max_seconds)
        write_json(args.batch, batch)
    else:
        batch = read_json(args.batch)
        master, state, checkpoint = apply_queue_batch(master, state, checkpoint, batch)
        write_json(MASTER, master)
        write_json(STATE, state)
        write_json(CHECKPOINT, checkpoint)
    print('TWITCH_OFFICIAL_QUEUE', args.phase, 'accepted', len(batch['records']), 'priority_pending', len(batch.get('follower_candidates', [])), 'stop', batch.get('stop_reason', 'complete'), flush=True)

if __name__ == "__main__":
    main()
