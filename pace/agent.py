"""Running commands as the agent user, and invoking Claude Code.

Every command runs through `sudo -n -u <agent>` with cwd `/`: sudo keeps the
caller's cwd and the agent can't stat PACE's directories. Claude's working
directory is set with `sudo -D`; git uses `-C`. With `user=None` commands run
as the current user (tests, fake claude).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

SCHEMA_DIR = Path(__file__).parent / "schemas"

Mode = Literal["read_only", "write"]


class AgentError(RuntimeError):
    pass


@dataclass
class RunResult:
    exit_code: int
    session_id: str | None
    structured_output: dict | None
    result_event: dict | None
    rate_limit: dict | None
    stderr: str

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0
            and self.result_event is not None
            and self.result_event.get("subtype") == "success"
            and isinstance(self.structured_output, dict)
        )


@dataclass
class Agent:
    user: str | None
    claude: str
    git_bin: str = "/usr/bin/git"
    timeout: float = 3600

    def _wrap(self, argv: list[str], cwd: str | None = None) -> tuple[list[str], str | None]:
        if self.user is None:
            return argv, cwd
        prefix = ["sudo", "-n", "-u", self.user, "-H"]
        if cwd:
            prefix += ["-D", cwd]
        return prefix + argv, "/"

    def git(self, *args: str, cwd: str | None = None, check: bool = True,
            stdout=subprocess.PIPE) -> subprocess.CompletedProcess:
        argv = [self.git_bin] + (["-C", cwd] if cwd else []) + list(args)
        argv, run_cwd = self._wrap(argv)
        proc = subprocess.run(argv, cwd=run_cwd or "/", stdout=stdout,
                              stderr=subprocess.PIPE)
        if check and proc.returncode != 0:
            raise AgentError(f"agent git {' '.join(args)}: {proc.stderr.decode().strip()}")
        return proc

    def claude_run(
        self,
        prompt: str,
        cwd: str,
        schema: str,
        mode: Mode,
        resume: str | None = None,
        model: str = "",
        log_path: Path | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> RunResult:
        schema_json = (SCHEMA_DIR / f"{schema}.json").read_text()
        argv = [self.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--json-schema", schema_json, "--disallowedTools", "AskUserQuestion"]
        if resume:
            argv += ["--resume", resume]
        if model:
            argv += ["--model", model]
        if mode == "read_only":
            argv += ["--permission-mode", "plan"]
        else:
            argv += ["--dangerously-skip-permissions"]
        argv, run_cwd = self._wrap(argv, cwd)

        session_id = structured = result = rate_limit = None
        log = open(log_path, "a") if log_path else None
        err = tempfile.TemporaryFile(mode="w+")
        proc = subprocess.Popen(argv, cwd=run_cwd, stdout=subprocess.PIPE,
                                stderr=err, text=True)
        # sudo relays the signal to claude
        killer = threading.Timer(self.timeout, proc.kill)
        killer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if log:
                    log.write(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = event.get("session_id") or session_id
                if event.get("type") == "rate_limit_event":
                    rate_limit = event.get("rate_limit_info")
                elif event.get("type") == "result":
                    result = event
                    structured = event.get("structured_output")
                if on_event:
                    on_event(event)
            proc.wait()
        finally:
            killer.cancel()
            if log:
                log.close()
        err.seek(0)
        stderr = err.read()
        err.close()
        return RunResult(proc.returncode, session_id, structured, result, rate_limit, stderr)
