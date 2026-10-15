"""Compatibility alias; implementation is shared by all collectors."""
import sys
from radar_core.storage import documents as _implementation
sys.modules[__name__] = _implementation
