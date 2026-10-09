"""The historical import path must expose the installed, pinned core."""

from pathlib import Path
import re

from radar_core.domain import twitch_admission as core
from scripts import twitch_steam_admission as compat


def test_legacy_exports_are_core_objects():
    assert len(compat.__all__) == 15
    for name in compat.__all__:
        assert getattr(compat, name) is getattr(core, name)


def test_dependency_is_an_immutable_core_revision():
    requirements = Path(__file__).resolve().parents[1] / "requirements-core.txt"
    assert re.search(r"\.git@[0-9a-f]{40}#subdirectory=packages/radar-core", requirements.read_text())
