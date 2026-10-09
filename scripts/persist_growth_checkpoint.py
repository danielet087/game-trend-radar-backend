"""Compatibility entry point for growth checkpoint merging and receipts."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_backend.domain.official_queue import aware_time, checked_numeric, valid_group_id64
from radar_backend.state.official_followers import read_state, save_state
from radar_backend.publication.growth import stamp_growth_publication
from radar_backend.state.growth_checkpoint import (
    COMMUNITY_FIELDS, FAILURE_STATUSES, _community, _deadline, _failure_time,
    _timestamp, _validate, merge_growth_checkpoint,
)
from radar_backend.jobs.growth_checkpoint import main, stamp_report


if __name__ == "__main__":
    main()
