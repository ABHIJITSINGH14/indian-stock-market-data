#!/usr/bin/env python3
"""Execute an AARU finite download plan safely and resumably.

Properties:
- sequential execution
- durable per-job state and logs
- disk reserve checks before each job
- refuses broad-scope jobs unless explicitly approved
- refuses database-writer jobs while auto_backfill holds its lock
- prevents concurrent AARU plan executors
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path, default: Mapping[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return dict(default)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object: {}".format(path))
    return value


@contextmanager
def exclusive_lock(path: Path, description: str) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "{} is already locked: {}".format(description, path)
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def ensure_reserve(path: Path, reserve_gib: float) -> float:
    free = disk_free_gib(path)
    if free < reserve_gib:
        raise RuntimeError(
            "Disk reserve not met: free={:.2f} GiB, required={:.2f} GiB"
            .format(free, reserve_gib)
        )
    return free


def selected_jobs(
    plan: Mapping[str, Any],
    job_ids: Optional[Sequence[str]],
) -> List[Mapping[str, Any]]:
    jobs = list(plan.get("jobs", []))
    if not job_ids:
        return jobs
    requested = set(job_ids)
    present = {str(job.get("id")) for job in jobs}
    missing = sorted(requested - present)
    if missing:
        raise ValueError("Unknown job IDs: {}".format(", ".join(missing)))
    return [job for job in jobs if str(job.get("id")) in requested]


def run_job(
    job: Mapping[str, Any],
    *,
    repo_root: Path,
    log_dir: Path,
    state: Dict[str, Any],
    state_path: Path,
    reserve_gib: float,
    dry_run: bool,
) -> int:
    job_id = str(job["id"])
    command = [str(part) for part in job.get("command", [])]
    if not command:
        raise RuntimeError("Job has no command: {}".format(job_id))

    free = ensure_reserve(repo_root, reserve_gib)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "{}.log".format(job_id)
    record = state.setdefault("jobs", {}).setdefault(job_id, {})
    record.update(
        {
            "status": "running" if not dry_run else "dry_run",
            "started_at": utcnow(),
            "finished_at": None,
            "return_code": None,
            "command": command,
            "log_path": str(log_path),
            "free_gib_before": round(free, 3),
            "broad_scope": bool(job.get("broad_scope")),
        }
    )
    atomic_json(state_path, state)

    if dry_run:
        print("[dry-run] {}: {}".format(job_id, " ".join(command)))
        record.update(
            {
                "status": "dry_run",
                "finished_at": utcnow(),
                "return_code": 0,
            }
        )
        atomic_json(state_path, state)
        return 0

    print("=== {} ===".format(job_id), flush=True)
    print("command: {}".format(" ".join(command)), flush=True)
    print("log: {}".format(log_path), flush=True)

    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n=== {} job={} ===\n".format(utcnow(), job_id))
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            return_code = process.wait()
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGINT)
            try:
                return_code = process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    return_code = process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    return_code = process.wait()
            record["interrupted"] = True

    record.update(
        {
            "status": "complete" if return_code == 0 else "failed",
            "finished_at": utcnow(),
            "return_code": int(return_code),
            "free_gib_after": round(disk_free_gib(repo_root), 3),
        }
    )
    atomic_json(state_path, state)
    return int(return_code)


def execute_plan(
    plan: Mapping[str, Any],
    *,
    repo_root: Path,
    state_path: Path,
    log_dir: Path,
    executor_lock: Path,
    auto_backfill_lock: Optional[Path],
    writer_lock: Path,
    job_ids: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    rerun_complete: bool = False,
    approve_broad_scope: bool = False,
    continue_on_error: bool = False,
) -> Dict[str, Any]:
    repo_root = repo_root.expanduser().resolve()
    if not repo_root.is_dir():
        raise FileNotFoundError(repo_root)
    reserve_gib = float(plan.get("reserve_gib", 12.0))
    jobs = selected_jobs(plan, job_ids)
    state = load_json(
        state_path,
        {
            "schema": "aaru.download-execution-state.v1",
            "plan_schema": plan.get("schema"),
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "jobs": {},
        },
    )
    state["updated_at"] = utcnow()
    state["plan_schema"] = plan.get("schema")
    atomic_json(state_path, state)

    summary = {
        "selected": len(jobs),
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "dry_run": dry_run,
    }

    with exclusive_lock(executor_lock, "AARU plan executor"):
        for job in jobs:
            job_id = str(job["id"])
            prior = state.get("jobs", {}).get(job_id, {})
            if prior.get("status") == "complete" and not rerun_complete:
                print("skip complete job: {}".format(job_id))
                summary["skipped"] += 1
                continue
            if (
                job.get("broad_scope")
                and not dry_run
                and not approve_broad_scope
            ):
                raise RuntimeError(
                    "Refusing broad-scope job without --approve-broad-scope: {}"
                    .format(job_id)
                )

            if dry_run:
                @contextmanager
                def dry_run_guard() -> Iterator[None]:
                    yield

                guard = dry_run_guard()
            elif job.get("writes_database"):
                @contextmanager
                def writer_guards() -> Iterator[None]:
                    if auto_backfill_lock is None:
                        raise RuntimeError(
                            "--auto-backfill-lock is required for "
                            "database-writer jobs"
                        )
                    with exclusive_lock(
                        auto_backfill_lock,
                        "auto_backfill (stop/checkpoint it before execution)",
                    ):
                        with exclusive_lock(
                            writer_lock, "stock_market.db writer"
                        ):
                            yield

                guard = writer_guards()
            else:
                @contextmanager
                def no_guard() -> Iterator[None]:
                    yield

                guard = no_guard()

            with guard:
                code = run_job(
                    job,
                    repo_root=repo_root,
                    log_dir=log_dir,
                    state=state,
                    state_path=state_path,
                    reserve_gib=reserve_gib,
                    dry_run=dry_run,
                )

            if code == 0:
                summary["completed"] += 1
            else:
                summary["failed"] += 1
                if not continue_on_error:
                    break

    state["updated_at"] = utcnow()
    state["last_summary"] = summary
    atomic_json(state_path, state)
    return summary


def parse_args(
    argv: Optional[Sequence[str]] = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--executor-lock", type=Path)
    parser.add_argument("--auto-backfill-lock", type=Path)
    parser.add_argument("--writer-lock", type=Path)
    parser.add_argument("--job", action="append", dest="job_ids")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rerun-complete", action="store_true")
    parser.add_argument("--approve-broad-scope", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    plan_path = args.plan.expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    base = plan_path.parent
    state_path = args.state or base / "download_execution_state.json"
    log_dir = args.log_dir or base / "download_logs"
    executor_lock = args.executor_lock or base / ".aaru-executor.lock"
    writer_lock = args.writer_lock or base / ".stock-market-writer.lock"

    summary = execute_plan(
        plan,
        repo_root=args.repo_root,
        state_path=state_path,
        log_dir=log_dir,
        executor_lock=executor_lock,
        auto_backfill_lock=args.auto_backfill_lock,
        writer_lock=writer_lock,
        job_ids=args.job_ids,
        dry_run=args.dry_run,
        rerun_complete=args.rerun_complete,
        approve_broad_scope=args.approve_broad_scope,
        continue_on_error=args.continue_on_error,
    )
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
