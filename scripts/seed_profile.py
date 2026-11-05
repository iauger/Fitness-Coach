"""
Seed / update the athlete_profile table (height, weight, FTP).

Safe to re-run. A bare run only seeds metrics that have no record at all, so it's a true
no-op once the baseline exists — it will never write a configured baseline back over a metric
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

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import migrate_db
from src.athlete.profile import set_metric, get_metric
from src.clock import today_local
from src.config import config


def defaults() -> dict:
    """
    `{metric: (value, effective_date, note)}` from the `profile` block of the config file.

    Height is static, so it is backdated to the first year of tracked history rather than to
    today — a height recorded as effective this morning would make every earlier w/kg figure
    unanswerable. Weight and FTP default to today, because those are measurements that start
    when you take them.

    A metric left unset in config is omitted entirely rather than defaulted to a number: a
    guessed FTP seeds a wrong power history, and every zone in every prescription is a
    percentage of it.
    """
    pf = config().profile
    year = config().athlete.history_start
    backdate = f"{year}-01-01" if year else None
    out = {}
    if pf.height_in is not None:
        out["height_in"] = (pf.height_in, backdate, None)
    if pf.weight_lbs is not None:
        out["weight_lbs"] = (pf.weight_lbs, None, None)
    if pf.ftp is not None:
        out["ftp"] = (pf.ftp, None, None)
    return out


def update(metric: str, value: float, effective_date: str = None, note: str = None) -> None:
    current = get_metric(metric, as_of=effective_date)
    if current == value:
        print(f"  {metric}: unchanged ({value})")
        return
    set_metric(metric, value, effective_date, note)
    eff = effective_date or today_local().isoformat()
    print(f"  {metric}: {current} -> {value}  (effective {eff})")


if __name__ == "__main__":
    migrate_db()
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
    seeds = defaults()
    # A flag applies even to a metric with no configured baseline — the flag *is* the value.
    for metric in ("height_in", "weight_lbs", "ftp"):
        default_value, default_date, default_note = seeds.get(metric, (None, None, None))
        override = overrides[metric]

        if override is not None:
            # Explicit flag — always record the new dated value.
            update(metric, override, args.effective_date, None)
            continue

        # No flag for this metric. Only seed the baseline if nothing is on record yet —
        # otherwise a bare re-run would write the configured baseline back over a value that was
        # deliberately changed (e.g. `--ftp 250` today, plain re-run tomorrow silently
        # reverting FTP to 238 and corrupting the dated history compliance scoring reads).
        if default_value is None:
            print(f"  {metric}: not configured (profile.{metric}) -> skipped")
        elif get_metric(metric) is None:
            update(metric, default_value, args.effective_date or default_date, default_note)
        else:
            print(f"  {metric}: already tracked ({get_metric(metric)}) -> left unchanged")
