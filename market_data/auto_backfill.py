"""Durable orchestration for unattended official-source backfills."""

import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence


GIB = 1024 ** 3


@dataclass(frozen=True)
class BackfillPhase:
    name: str
    command: Sequence[str]
    success_interval: int = 24 * 60 * 60
    failure_interval: int = 6 * 60 * 60


def phase_commands(
    python: str,
    database_url: str,
    today: Optional[date] = None,
    manifest_path: Optional[Path] = None,
    raw_root: Optional[Path] = None,
) -> List[BackfillPhase]:
    today = today or date.today()
    root = str(Path(__file__).resolve().parents[1])
    fundamentals = str(Path(root) / "scripts" / "backfill_fundamentals.py")
    prices = str(Path(root) / "scripts" / "backfill.py")
    disclosures = str(Path(root) / "scripts" / "collect_disclosures.py")
    local_archives = str(Path(root) / "scripts" / "import_local_archives.py")
    common_price = [
        "--database-url", database_url,
        "--timeout", "45",
        "--retries", "4",
        "--progress-interval", "60",
    ]
    common_fundamental = [
        "--database-url", database_url,
        "--start-date", "2007-02-01",
        "--end-date", today.isoformat(),
        "--datasets", "financial_results", "shareholding",
        "--window-days", "30",
        "--retry-failed",
        "--timeout", "45",
        "--retries", "4",
    ]
    recent_start = (today - timedelta(days=45)).isoformat()
    phases = []
    if manifest_path is not None and raw_root is not None:
        phases.append(
            BackfillPhase(
                "local_manifest_catalog",
                [
                    python,
                    local_archives,
                    "--database-url", database_url,
                    "--manifest", str(manifest_path),
                    "--raw-root", str(raw_root),
                    "--catalog-all",
                ],
                success_interval=6 * 60 * 60,
                failure_interval=60 * 60,
            )
        )
    phases.extend([
        BackfillPhase(
            "nse_prices",
            [python, prices, "backfill", "--exchange", "nse", "--workers", "2",
             "--request-delay", "2"] + common_price,
        ),
        BackfillPhase(
            "bse_prices",
            [python, prices, "backfill", "--exchange", "bse", "--workers", "1",
             "--request-delay", "4"] + common_price,
            failure_interval=12 * 60 * 60,
        ),
        BackfillPhase(
            "nse_fundamentals",
            [python, fundamentals, "backfill", "--exchange", "nse",
             "--workers", "2", "--request-delay", "2"] + common_fundamental,
        ),
        BackfillPhase(
            "bse_fundamentals",
            [python, fundamentals, "backfill", "--exchange", "bse",
             "--workers", "1", "--request-delay", "4"] + common_fundamental,
            failure_interval=12 * 60 * 60,
        ),
        BackfillPhase(
            "recent_disclosures",
            [
                python,
                disclosures,
                "--exchange", "nse",
                "--datasets", "corporate_actions", "board_meetings", "pit",
                "sast", "fii_dii",
                "--start-date", recent_start,
                "--end-date", today.isoformat(),
                "--window-days", "15",
                "--request-delay", "2",
                "--timeout", "45",
                "--retries", "4",
                "--database-url", database_url,
            ],
        ),
    ])
    return phases


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, object]:
        if not self.path.exists():
            return {"phases": {}}
        with self.path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or not isinstance(value.get("phases"), dict):
            raise ValueError("Invalid auto-backfill state file: {}".format(self.path))
        return value

    def save(self, state: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(str(temporary), str(self.path))


def retry_delay(phase: BackfillPhase, failures: int) -> int:
    exponent = max(0, min(failures - 1, 3))
    return min(24 * 60 * 60, phase.failure_interval * (2 ** exponent))


def sqlite_reusable_bytes(database_url: str) -> int:
    """Return bytes already allocated to reusable SQLite freelist pages."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return 0
    path = Path(database_url[len(prefix) :]).expanduser().resolve()
    if not path.exists():
        return 0
    connection = sqlite3.connect(
        "file:{}?mode=ro".format(path), uri=True, timeout=5
    )
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        return page_size * pages
    finally:
        connection.close()


class AutoBackfillRunner:
    def __init__(
        self,
        phases: Sequence[BackfillPhase],
        state_store: StateStore,
        lock_path: Path,
        minimum_free_bytes: int,
        poll_interval: int = 300,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        reusable_bytes: Callable[[], int] = lambda: 0,
        minimum_physical_bytes: Optional[int] = None,
    ):
        self.phases = list(phases)
        self.state_store = state_store
        self.lock_path = lock_path
        self.minimum_free_bytes = minimum_free_bytes
        self.poll_interval = poll_interval
        self.clock = clock
        self.sleeper = sleeper
        self.reusable_bytes = reusable_bytes
        self.minimum_physical_bytes = (
            min(minimum_free_bytes, 8 * GIB)
            if minimum_physical_bytes is None
            else minimum_physical_bytes
        )
        self._child: Optional[subprocess.Popen] = None
        self._stopping = False

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("auto-backfill is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def stop(self, *_args) -> None:
        self._stopping = True
        if self._child is not None and self._child.poll() is None:
            os.killpg(self._child.pid, signal.SIGINT)

    def run(self, once: bool = False) -> None:
        with self.lock():
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)
            while not self._stopping:
                state = self.state_store.load()
                phase_state = state.setdefault("phases", {})
                now = self.clock()
                due = [
                    phase for phase in self.phases
                    if float(phase_state.get(phase.name, {}).get("next_run", 0)) <= now
                ]
                if not due:
                    if once:
                        return
                    next_run = min(
                        float(item.get("next_run", now + self.poll_interval))
                        for item in phase_state.values()
                    )
                    self.sleeper(max(1, min(self.poll_interval, next_run - now)))
                    continue
                for phase in due:
                    if self._stopping:
                        return
                    free, reusable = self.storage_headroom()
                    if self.disk_guard_reached(free, reusable):
                        phase_state[phase.name] = {
                            **phase_state.get(phase.name, {}),
                            "last_status": "low_disk",
                            "last_free_bytes": free,
                            "last_reusable_bytes": reusable,
                            "last_effective_bytes": free + reusable,
                            "next_run": self.clock() + self.poll_interval,
                        }
                        self.state_store.save(state)
                        continue
                    result = self.run_phase(phase)
                    previous = phase_state.get(phase.name, {})
                    failures = 0 if result == 0 else int(previous.get("failures", 0)) + 1
                    delay = (
                        phase.success_interval
                        if result == 0
                        else retry_delay(phase, failures)
                    )
                    phase_state[phase.name] = {
                        "failures": failures,
                        "last_exit_code": result,
                        "last_finished": self.clock(),
                        "last_status": "complete" if result == 0 else "retry",
                        "next_run": self.clock() + delay,
                    }
                    self.state_store.save(state)
                if once:
                    return

    def run_phase(self, phase: BackfillPhase) -> int:
        print("auto-backfill starting phase {}".format(phase.name), flush=True)
        self._child = subprocess.Popen(list(phase.command), start_new_session=True)
        try:
            while self._child.poll() is None:
                free, reusable = self.storage_headroom()
                if self.disk_guard_reached(free, reusable):
                    print(
                        "auto-backfill stopping {}: disk guard reached".format(
                            phase.name
                        ),
                        flush=True,
                    )
                    os.killpg(self._child.pid, signal.SIGINT)
                    try:
                        self._child.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        os.killpg(self._child.pid, signal.SIGTERM)
                        try:
                            self._child.wait(timeout=60)
                        except subprocess.TimeoutExpired:
                            os.killpg(self._child.pid, signal.SIGKILL)
                            self._child.wait()
                    return 75
                self.sleeper(min(60, self.poll_interval))
            return int(self._child.returncode or 0)
        finally:
            self._child = None

    @staticmethod
    def free_bytes() -> int:
        stats = os.statvfs(".")
        return stats.f_bavail * stats.f_frsize

    def storage_headroom(self) -> tuple:
        return self.free_bytes(), max(0, int(self.reusable_bytes()))

    def disk_guard_reached(self, free: int, reusable: int) -> bool:
        return (
            free < self.minimum_physical_bytes
            or free + reusable < self.minimum_free_bytes
        )
