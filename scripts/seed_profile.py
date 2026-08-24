"""
Seed / update the athlete_profile table (height, weight, FTP).

Safe to re-run. A bare run only seeds metrics that have no record at all, so it's a true
no-op once the baseline exists — it will never write a DEFAULTS value back over a metric
you've since changed via a flag. Flags always record a new dated entry (skipped only if the
value matches what's already effective on that date).

Usage:
    python scripts/seed_profile.py                          # establish baseline (once)
    python scripts/seed_profile.py --ftp 245                 # FTP changed, effective today
    python scripts/seed_profile.py --weight 188 --ftp 245
    python scripts/seed_profile.py --ftp 250 --effective-date 2026-09-01
"""

import sys
import argparse
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import init_db, migrate_db
from src.athlete.profile import set_metric, get_metric

# (default value, default effective_date, default note) — height is static, so it's
# backdated to the start of the athlete's tracked history; weight/FTP default to today.
DEFAULTS = {
    "height_in":  (75.0, "2017-01-01", "6'3\""),
    "weight_lbs": (190.0, None, None),
    "ftp":        (238.0, None, None),
}


def update(metric: str, value: float, effective_date: str = None, note: str = None) -> None:
    current = get_metric(metric, as_of=effective_date)
    if current == value:
        print(f"  {metric}: unchanged ({value})")
        return
    set_metric(metric, value, effective_date, note)
    eff = effective_date or date.today().isoformat()
    print(f"  {metric}: {current} -> {value}  (effective {eff})")


if __name__ == "__main__":
    init_db()
    migrate_db()

    parser = argparse.ArgumentParser()
    parser.add_argument("--height-in", type=float, help="Height in inches")
    parser.add_argument("--weight", type=float, help="Weight in lbs")
    parser.add_argument("--ftp", type=float, help="FTP in watts")
    parser.add_argument("--effective-date", help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    overrides = {
        "height_in": args.height_in,
        "weight_lbs": args.weight,
        "ftp": args.ftp,
    }

    print("Updating athlete profile...")
    for metric, (default_value, default_date, default_note) in DEFAULTS.items():
        override = overrides[metric]

        if override is not None:
            # Explicit flag — always record the new dated value.
            update(metric, override, args.effective_date, None)
            continue

        # No flag for this metric. Only seed the baseline if nothing is on record yet —
        # otherwise a bare re-run would write DEFAULTS back over a value that was
        # deliberately changed (e.g. `--ftp 250` today, plain re-run tomorrow silently
        # reverting FTP to 238 and corrupting the dated history compliance scoring reads).
        if get_metric(metric) is None:
            update(metric, default_value, args.effective_date or default_date, default_note)
        else:
            print(f"  {metric}: already tracked ({get_metric(metric)}) -> left unchanged")
