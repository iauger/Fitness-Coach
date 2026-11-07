"""
A change the coach would like the athlete to consider, offered as a button.

The coach cannot change a setting. `suggest_change` validates a proposal against what is true
now and hands it back to be shown as a card with Apply and Not now; nothing is written until the
athlete presses Apply, and that press goes through the same guarded, validated settings route
as the Settings screen. Applying a setting moves the *limits*; the ladder is then re-derived
through its own preview (the Ladder screen) where the athlete sees every rung that would change
before keeping it. The suggestion stops at the setting, on purpose.

Only a few settings can be suggested, and only ones where the coach can have a view the data
supports: how many days a week the ladder may ask for, and the longest single ride. Everything
is checked here so the model hears "no" for a nonsense proposal instead of showing the athlete a
button that will fail.
"""

from src import config as cfg
from src.planning.constants import available_days, max_ride_days

# setting -> (what the athlete calls it, lowest, highest). The whole whitelist.
SETTINGS = {
    "plan.max_ride_days": ("rides a week, at most", 1, 7),
    "plan.max_ride_min": ("longest ride on any day, in minutes", 30, 360),
}


def _current(setting: str):
    plan = cfg.load(create_if_missing=False).plan
    return {"plan.max_ride_days": plan.max_ride_days, "plan.max_ride_min": plan.max_ride_min}[setting]


def suggest_change(setting: str, value: int, reason: str) -> dict:
    """Validate a proposal and return the card to show. Writes nothing."""
    if setting not in SETTINGS:
        return {"error": f"only these can be suggested: {', '.join(SETTINGS)}"}
    label, low, high = SETTINGS[setting]
    if isinstance(value, bool) or not isinstance(value, int):
        return {"error": "value must be a whole number"}
    if not low <= value <= high:
        return {"error": f"{label} must be between {low} and {high}"}
    reason = " ".join((reason or "").split())
    if not reason:
        return {"error": "say why, in a sentence the athlete will read beside the button"}

    now = _current(setting)
    if setting == "plan.max_ride_min" and now is None:
        return {"error": "there is no limit on ride length to change"}
    if value == now:
        return {"error": f"{label} is already {now}"}
    if setting == "plan.max_ride_days":
        days = available_days()
        if days is not None and value > len(days):
            return {"error": f"only {len(days)} days are available for training, so {value} rides "
                             f"cannot fit; the athlete would have to add available days first"}
    return {"status": "shown", "setting": setting, "label": label, "from": now, "to": value,
            "reason": reason[:300],
            "note": "Shown to the athlete as a button. It has NOT been applied and may never be: "
                    "do not say it has changed, and do not suggest it again this conversation."}
