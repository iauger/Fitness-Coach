"""
Change settings in `config/athlete.yaml` without losing what is written around them.

The comments in that file are most of its documentation, so a save from the UI must not
flatten them: this uses ruamel.yaml's round-trip mode, not PyYAML. Reading stays on PyYAML in
`src/config.py`; this module only writes.

    update({"plan.max_ride_min": 90, "plan.available_days": ["tue", "thu", "sat", "sun"]})
    update({"plan.max_ride_min": None})         # None removes the key: back to "no limit"

A change is applied to the text, the *result* is parsed and validated by `config.parse`, and
only then is the file replaced, atomically. So an invalid combination is refused with the
loader's own message and the file on disk is untouched, and a crash mid-write cannot leave half
a config. The previous contents are kept beside it as `athlete.yaml.bak`.

Only paths the loader knows are accepted: a typo that PyYAML would have carried silently is
refused by `_build`, which is the same check every load makes.
"""

import io
import os
from datetime import date
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from src import config as cfg


class ConfigWriteError(cfg.ConfigError):
    pass


def _yaml() -> YAML:
    y = YAML()                       # round-trip: keeps comments, order and quoting
    y.preserve_quotes = True
    y.width = 100
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _plain(value: Any) -> Any:
    """Short lists and maps stay on one line, as the example writes them: [3.5, 4.0]."""
    if isinstance(value, (list, tuple)):
        seq = CommentedSeq(_plain(v) for v in value)
        seq.fa.set_flow_style()
        return seq
    if isinstance(value, dict):
        m = CommentedMap((k, _plain(v)) for k, v in value.items())
        m.fa.set_flow_style()
        return m
    return value


def _set(root: CommentedMap, dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    node = root
    for key in parents:
        child = node.get(key)
        if child is None:
            if value is None:
                return                       # removing something under a section never there
            child = CommentedMap()
            node[key] = child
        if not isinstance(child, CommentedMap):
            raise ConfigWriteError(f"{key!r} in {dotted!r} is not a section of settings")
        node = child
    if value is None:
        node.pop(leaf, None)
    else:
        node[leaf] = _plain(value)


def apply(text: str, changes: dict[str, Any]) -> str:
    """The new file text. Pure, so it is tested without touching a file."""
    y = _yaml()
    root = y.load(text)
    if not isinstance(root, CommentedMap):
        raise ConfigWriteError("the configuration is not a block of settings")
    for dotted, value in changes.items():
        if isinstance(value, str) and len(value) == 10 and value[4] == "-" == value[7]:
            try:                              # YAML dates are bare; a string would be quoted
                value = date.fromisoformat(value)
            except ValueError:
                pass
        _set(root, dotted, value)
    out = io.StringIO()
    y.dump(root, out)
    return out.getvalue()


def update(changes: dict[str, Any], path: Path | None = None) -> cfg.Config:
    """Apply, validate, then replace the file. Returns the new configuration."""
    path = Path(path) if path else cfg.CONFIG_PATH
    if path.resolve() == cfg.EXAMPLE_PATH.resolve():
        # The example is committed documentation. Under test the default path *is* the example,
        # so a forgotten explicit path would otherwise rewrite it.
        raise ConfigWriteError("refusing to write the committed example config")
    if not path.exists():
        raise ConfigWriteError(f"no configuration at {path}; the first run creates it")
    old = path.read_text(encoding="utf-8")
    new = apply(old, changes)
    try:
        result = cfg.parse(new)              # nothing is written unless this passes
    except cfg.ConfigError as exc:
        raise ConfigWriteError(str(exc)) from None
    if new == old:
        return result
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8", newline="\n")
    path.with_name(path.name + ".bak").write_text(old, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    cfg.reset()
    return result
