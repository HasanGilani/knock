"""Settings, loaded from ``$KNOCK_HOME/config.json`` with documented defaults.

Repository content never changes these settings. A repository may only *add*
exclusions, via a ``.knockignore`` file at its root.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_HOME = Path.home() / ".knock"


@dataclass
class Config:
    home: Path = DEFAULT_HOME
    # Discovery
    max_file_bytes: int = 512_000
    max_files: int = 50_000
    exclude: list[str] = field(default_factory=list)
    # Git history
    commit_history_limit: int = 300
    # Response budgets (characters; roughly 4 characters per token)
    search_max_chars: int = 4_000
    context_max_chars: int = 8_000
    source_max_lines: int = 200
    source_max_chars: int = 16_000
    max_results: int = 25
    # Agent-recorded knowledge
    record_max_chars: int = 2_000
    # Query cache
    cache_max_entries: int = 1_000
    cache_max_bytes: int = 16 * 1024 * 1024
    # Freshness checks stat at most this many files per query
    freshness_stat_limit: int = 20_000
    freshness_ttl_seconds: float = 5.0
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.home / "knock.db"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["home"] = str(self.home)
        d["db_path"] = str(self.db_path)
        return d


def load_config(home: str | os.PathLike[str] | None = None) -> Config:
    home_path = Path(home or os.environ.get("KNOCK_HOME") or DEFAULT_HOME).expanduser()
    cfg = Config(home=home_path)
    path = home_path / "config.json"
    if path.is_file():
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object")
        known = {f.name: f for f in fields(Config) if f.name != "home"}
        for key, value in data.items():
            if key not in known:
                raise ValueError(f"unknown setting {key!r} in {path}")
            default = getattr(cfg, key)
            ok = isinstance(value, type(default)) or (isinstance(default, float) and isinstance(value, int))
            if not ok or isinstance(value, bool):
                raise ValueError(f"setting {key!r} in {path} must be {type(default).__name__}")
            if key == "exclude" and not all(isinstance(v, str) for v in value):
                raise ValueError(f"setting 'exclude' in {path} must be a list of strings")
            setattr(cfg, key, value)
    if os.environ.get("KNOCK_LOG_LEVEL"):
        cfg.log_level = os.environ["KNOCK_LOG_LEVEL"].upper()
    return cfg
