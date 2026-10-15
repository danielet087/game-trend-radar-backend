"""The owner uses its checked-out core; historical imports retain identity."""

from pathlib import Path
import re

from radar_core.domain import twitch_admission as core
from scripts import twitch_steam_admission as compat


def test_legacy_exports_are_core_objects():
    assert len(compat.__all__) == 15
    for name in compat.__all__:
        assert getattr(compat, name) is getattr(core, name)


def test_owner_dependency_uses_the_same_checked_out_revision():
    requirements = Path(__file__).resolve().parents[1] / "requirements-core.txt"
    entries = [line.strip() for line in requirements.read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    assert entries == ["./packages/radar-core"]
    assert (requirements.parent / entries[0] / "pyproject.toml").is_file()
