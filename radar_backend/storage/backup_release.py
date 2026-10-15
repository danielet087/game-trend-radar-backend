"""Compatibility alias; implementation is shared by all collectors."""
import sys
from radar_core.storage import backup_release as _implementation
sys.modules[__name__] = _implementation
