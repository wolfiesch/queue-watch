"""Sources: configured adapter instances, loaded from TOML or command-line flags.

[[source]]
id = "ci"                     # unique; required
adapter = "github-actions"    # required; see queue_watch.adapters.registry()
label = "CI"                  # optional; defaults to the adapter's label
interval = 15                 # optional refresh seconds
repo = "owner/repo"           # every other key is an adapter option
"""

from __future__ import annotations

import inspect
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .adapters import Adapter, registry
from .model import validate_snapshot


class ConfigError(ValueError):
    """The source configuration cannot be used."""


@dataclass
class Source:
    id: str
    label: str
    interval: float
    adapter: Adapter

    async def snapshot(self) -> dict:
        return validate_snapshot(await self.adapter.snapshot())


INJECTED = frozenset({"config_dir"})


def build(entry: dict, where: str, config_dir: Path | None = None) -> Source:
    options = dict(entry)
    source_id = options.pop("id", None)
    name = options.pop("adapter", None)
    if not isinstance(source_id, str) or not source_id:
        raise ConfigError(f"{where}: id must be a non-empty string")
    adapters = registry()
    if name not in adapters:
        raise ConfigError(
            f"{where}: adapter must be one of {', '.join(sorted(adapters))}, got {name!r}"
        )
    kind = adapters[name]
    label = options.pop("label", kind.label)
    interval = options.pop("interval", kind.interval)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval < 1:
        raise ConfigError(f"{where}: interval must be a number of seconds >= 1")
    parameters = inspect.signature(kind.__init__).parameters
    accepted = {key: p for key, p in parameters.items() if key not in {"self", *INJECTED}}
    unknown = sorted(set(options) - set(accepted))
    if unknown:
        raise ConfigError(
            f"{where}: {name} does not accept {', '.join(unknown)}; "
            f"options are {', '.join(accepted)}"
        )
    missing = [
        key
        for key, parameter in accepted.items()
        if parameter.default is inspect.Parameter.empty and key not in options
    ]
    if missing:
        raise ConfigError(f"{where}: {name} needs {', '.join(missing)}")
    if "config_dir" in parameters:
        options["config_dir"] = config_dir
    try:
        adapter = kind(**options)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{where}: {error}") from error
    return Source(source_id, str(label), float(interval), adapter)


def load(path: Path) -> list[Source]:
    try:
        text = path.read_text()
    except OSError as error:
        raise ConfigError(f"{path}: {error}") from error
    return parse(text, path)


def parse(text: str, path: Path) -> list[Source]:
    """Sources from TOML ``text`` that was read from ``path``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    entries = data.get("source")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path}: define at least one [[source]] table")
    sources = [
        build(entry, f"{path} source {index + 1}", path.parent.resolve())
        for index, entry in enumerate(entries)
    ]
    ids = [source.id for source in sources]
    duplicates = sorted({source_id for source_id in ids if ids.count(source_id) > 1})
    if duplicates:
        raise ConfigError(f"{path}: duplicate source id {', '.join(duplicates)}")
    return sources
