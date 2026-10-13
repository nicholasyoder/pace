"""Global settings from `config.toml` (§5). Project settings live in the DB.

The file is found at $PACE_CONFIG, else ./config.toml; every key is optional.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from pace import windows


@dataclass
class Config:
    data_dir: Path = Path("/var/lib/pace")
    bind: str = "127.0.0.1"
    port: int = 8420
    agent_user: str = "pace-agent"   # empty: run claude/git as the PACE user (tests)
    agent_home: Path = Path("/home/pace-agent")
    claude: str = "/home/pace-agent/.local/bin/claude"
    run_timeout_minutes: int = 60
    concurrency: int = 2
    run_windows: list[str] = field(default_factory=list)
    rate_limit_threshold: float = 0.9  # back off at this 5h / 7d utilization (§8.1)
    ntfy_url: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pace.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("PACE_CONFIG", "config.toml"))
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    known = {f.name: f for f in fields(Config)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    for k in ("data_dir", "agent_home"):
        if k in raw:
            raw[k] = Path(raw[k])
    if "rate_limit_threshold" in raw:
        t = raw["rate_limit_threshold"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 1:
            raise ValueError(f"{path}: rate_limit_threshold must be a number in (0, 1]")
        raw["rate_limit_threshold"] = float(t)
    if "run_windows" in raw:
        rw = raw["run_windows"]
        if not isinstance(rw, list) or not all(isinstance(w, str) for w in rw):
            raise ValueError(f"{path}: run_windows must be a list of \"HH:MM-HH:MM\" strings")
        for w in rw:
            try:
                windows.parse(w)
            except ValueError as e:
                raise ValueError(f"{path}: run_windows: {e}") from None
    return Config(**raw)
