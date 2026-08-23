"""
Generate all visualization charts and the single-page dashboard.

Usage:
    python scripts/generate_charts.py           # dashboard only
    python scripts/generate_charts.py --open    # open dashboard in browser
    python scripts/generate_charts.py --also-individual  # also write per-chart files
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.visualizations import pmc, load, recovery, volume, distribution
from src.visualizations.dashboard import write_dashboard
from src.visualizations.calendar_view import write_calendar

CHARTS_DIR = Path(__file__).parent.parent / "data" / "charts"

CHARTS = [
    ("pmc.html",          "PMC",               lambda: pmc.build(weeks=52)),
    ("load.html",         "Weekly Load",        lambda: load.build(weeks=12)),
    ("recovery.html",     "Recovery Signals",   lambda: recovery.build(days=30)),
    ("volume.html",       "Annual Volume",      lambda: volume.build()),
    ("distribution.html", "Sport Distribution", lambda: distribution.build(days=90)),
]


def generate(open_browser: bool = False, also_individual: bool = False) -> None:
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    if also_individual:
        for filename, title, builder in CHARTS:
            print(f"  building {title}...", end=" ", flush=True)
            fig = builder()
            out = CHARTS_DIR / filename
            fig.write_html(str(out), include_plotlyjs="cdn", full_html=True)
            print(f"done  ->  {out.name}")

    print("  building dashboard...", end=" ", flush=True)
    out = write_dashboard(open_browser=open_browser)
    print(f"done  ->  {out.name}")

    print("  building calendar...", end=" ", flush=True)
    cal_out = write_calendar(open_browser=open_browser)
    print(f"done  ->  {cal_out.name}")

    print(f"\nDashboard: {out}")
    print(f"Calendar:  {cal_out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true",
                        help="Open dashboard in browser after generating")
    parser.add_argument("--also-individual", action="store_true",
                        help="Also write per-chart HTML files")
    args = parser.parse_args()
    generate(open_browser=args.open, also_individual=args.also_individual)
