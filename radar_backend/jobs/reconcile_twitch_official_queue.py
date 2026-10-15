"""Twitch queue receipt CLI using canonical intake and queue composition."""
from __future__ import annotations
import argparse
import os
import subprocess
import tempfile
from pathlib import Path
from radar_backend.adapters.twitch_intake import (
    collect, dispatch, read_json, write_json, apply_queue_batch,
    CHECKPOINT, MASTER, STATE,
)

FRONTEND_SOURCE = 'danielet087/game-trend-radar@main'
TWITCH_INPUTS = ('data/twitch_steam_discovery.json', 'data/twitch_tracking.json',
                'data/steam_upcoming.json', 'data/twitch_steam_mapping.json')


def collect_from_database(master, state, *, caches, max_seconds, primary=None):
    """Freeze and pin DB evidence before any metadata request or admission."""
    if primary is None:
        from radar_core.storage import primary
    from radar_core.storage import primary_store
    head, files = primary.read_files(FRONTEND_SOURCE, paths=TWITCH_INPUTS)
    if set(files) != set(TWITCH_INPUTS):
        raise ValueError('The authoritative Twitch snapshot is incomplete')
    proof = {'source_database_revision': head['manifest_digest'],
             'source_database_generation': head['generation'],
             'source_database_namespace': FRONTEND_SOURCE}
    primary_store.pin_evidence(primary.client(), FRONTEND_SOURCE,
        revision=head['manifest_digest'], paths=TWITCH_INPUTS,
        evidence_id='steam-twitch-' + head['manifest_digest'])
    with tempfile.TemporaryDirectory(prefix='radar-twitch-primary-') as directory:
        root = Path(directory)
        for name, raw in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        return collect(root, proof, master, state, caches=caches, max_seconds=max_seconds)

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
        paths = [Path('data/steam_followers_cache.json'), Path('data/steam_followers_checkpoint.json'), Path('experiments/steam_official_followers_20260922/checkpoint.json'), Path('experiments/steam_official_nearfirst_20260922/checkpoint.json')]
        caches = [checkpoint, *(read_json(p) for p in paths if p.is_file())]
        from radar_core.storage import primary
        if primary.enabled():
            batch = collect_from_database(master, state, caches=caches, max_seconds=args.max_seconds)
        else:
            if args.frontend_path is None or args.frontend_commit is None:
                parser.error('collect requires an immutable frontend snapshot')
            actual = subprocess.check_output(['git', '-C', str(args.frontend_path), 'rev-parse', 'HEAD'], text=True).strip()
            if actual != args.frontend_commit:
                parser.error('frontend snapshot commit mismatch')
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
