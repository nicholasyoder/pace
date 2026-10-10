"""PACE server: `uv run python -m pace` (reads config.toml, see pace/config.py)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pace import config as config_mod
from pace.agent import Agent
from pace.bus import Bus
from pace.config import Config
from pace.db import DB
from pace.runs import Runner
from pace.scheduler import Scheduler
from pace.web import router


def create_app(cfg: Config, runner: Runner | None = None, scheduler: bool = True) -> FastAPI:
    if runner is None:
        db = DB(cfg.db_path)
        agent = Agent(user=cfg.agent_user or None, claude=cfg.claude,
                      timeout=cfg.run_timeout_minutes * 60)
        runner = Runner(db, cfg, agent, Bus())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner.bus.attach(asyncio.get_running_loop())
        runner.recover()
        sched = Scheduler(runner) if scheduler else None
        if sched:
            sched.start()
        try:
            yield
        finally:
            if sched:
                await sched.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg, app.state.db, app.state.runner = cfg, runner.db, runner
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(router)
    return app


def main() -> None:
    cfg = config_mod.load()
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(cfg), host=cfg.bind, port=cfg.port, log_level="info")
