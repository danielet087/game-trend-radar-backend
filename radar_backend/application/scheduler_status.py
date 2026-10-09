"""Read inputs, project the scheduler dashboard and save its destination only."""
from __future__ import annotations

def export_status(*, output, now=None, clock, read, is_file, save, build_status,
                  checkpoint_path, frozen, eligible_path, prefilter_path,
                  official_cache_path, original_official_path, candidate_state_path,
                  twitch_state_path):
    observed = now or clock()
    optional = lambda path: read(path) if is_file(path) else {}
    status = build_status(
        read(checkpoint_path), read(frozen / "source_queue.json"),
        read(frozen / "checkpoint.json"), read(frozen / "source_unresolved.json"),
        read(eligible_path), read(prefilter_path), read(official_cache_path),
        read(original_official_path), now=observed,
        candidate_state=optional(candidate_state_path),
        twitch_state=optional(twitch_state_path),
    )
    save(output, status)
    return status
