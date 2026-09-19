"""Extended bounded read-only probe reusing the 60-title pipeline safely."""
from pathlib import Path

import scripts.probe_steam_bulk_coverage as probe


def select_205(cache):
    high = sorted(
        (k for k,v in cache.items() if int(v["followers"]) >= 5000),
        key=lambda k: int(cache[k]["followers"]), reverse=True,
    )[:45]
    middle = sorted(
        (k for k,v in cache.items() if 100 <= int(v["followers"]) < 5000),
        key=lambda k: int(cache[k]["followers"]), reverse=True,
    )[:80]
    low = sorted(
        (k for k,v in cache.items() if int(v["followers"]) < 100),
        key=lambda k: int(cache[k]["followers"]), reverse=True,
    )[:80]
    return high + middle + low


if __name__ == "__main__":
    probe.pick = select_205
    probe.OUTPUT = Path("output/steam_bulk_stress_probe.json")
    probe.main()
