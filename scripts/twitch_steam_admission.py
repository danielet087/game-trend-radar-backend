"""Compatibility imports for the versioned Radar Core admission rules.

Collectors keep their existing import paths; validation has one shared owner.
Install the repository's requirements-core.txt before using this module.
"""

from radar_core.domain.twitch_admission import (
    EVIDENCE_SOURCES,
    METHOD,
    TAIPEI,
    TW_STORE_DATE_AUTHORITY,
    TW_STORE_DATE_PROVIDER,
    aware_time,
    decimal_id,
    has_taiwan_store_date_authority,
    has_twitch_admission,
    is_twitch_qualified,
    normalize_twitch_admission,
    preserve_twitch_admission,
    resolve_store_release_day,
    valid_enrollment,
    validate_twitch_snapshot,
)

__all__ = [
    "EVIDENCE_SOURCES", "METHOD", "TAIPEI", "TW_STORE_DATE_AUTHORITY",
    "TW_STORE_DATE_PROVIDER", "aware_time", "decimal_id",
    "has_taiwan_store_date_authority", "has_twitch_admission",
    "is_twitch_qualified", "normalize_twitch_admission",
    "preserve_twitch_admission", "resolve_store_release_day",
    "valid_enrollment", "validate_twitch_snapshot",
]
