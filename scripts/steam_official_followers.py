"""Compatibility exports for the layered official Followers boundary.

The public classes are the same objects used by both hourly and growth jobs.
New code imports their adapter/state/domain owners directly.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
from typing import Callable
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

from radar_backend.domain.official_queue import (
    GROUP_BASE, TAIPEI, FollowerOutcome, OfficialObservation, aware_time,
    checked_numeric, group_to_gid, steam_429_cooldown, valid_group_id64,
)
from radar_backend.state.official_followers import (
    CooldownStore, OfficialFollowerCache, read_state, save_state,
)
from radar_backend.adapters.official_followers import (
    OfficialFollowerClient, parse_official_xml,
)
