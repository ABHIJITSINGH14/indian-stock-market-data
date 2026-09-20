from pathlib import Path

import pytest

from scripts.aaru_execute_plan import execute_plan


def test_executor_runs_sequentially_and_resumes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = {
        "schema": "aaru.download-plan.v2",
        "reserve_gib": 0,
        "jobs": [
            {
                "id": "first",
                "command": [
                    "python3", "-c",
                    "from pathlib import Path; Path('order.txt').write_text('1')",
                ],
                "writes_database": False,
                "broad_scope": False,
            },
            {
                "id": "second",
                "command": [
                    "python3", "-c",
                    "from pathlib import Path; p=Path('order.txt'); p.write_text(p.read_text()+'2')",
                ],
                "writes_database": False,
                "broad_scope": False,
            },
        ],
    }
    state = tmp_path / "state.json"
    summary = execute_plan(
        plan,
        repo_root=repo,
        state_path=state,
        log_dir=tmp_path / "logs",
        executor_lock=tmp_path / "executor.lock",
        auto_backfill_lock=None,
        writer_lock=tmp_path / "writer.lock",
    )
    assert summary["completed"] == 2
    assert (repo / "order.txt").read_text() == "12"

    again = execute_plan(
        plan,
        repo_root=repo,
        state_path=state,
        log_dir=tmp_path / "logs",
        executor_lock=tmp_path / "executor.lock",
        auto_backfill_lock=None,
        writer_lock=tmp_path / "writer.lock",
    )
    assert again["skipped"] == 2
    assert (repo / "order.txt").read_text() == "12"


def test_executor_refuses_broad_scope_without_approval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = {
        "reserve_gib": 0,
        "jobs": [
            {
                "id": "broad",
                "command": ["python3", "-c", "print('x')"],
                "writes_database": False,
                "broad_scope": True,
            }
        ],
    }
    with pytest.raises(RuntimeError, match="broad-scope"):
        execute_plan(
            plan,
            repo_root=repo,
            state_path=tmp_path / "state.json",
            log_dir=tmp_path / "logs",
            executor_lock=tmp_path / "executor.lock",
            auto_backfill_lock=None,
            writer_lock=tmp_path / "writer.lock",
        )


def test_writer_requires_auto_backfill_lock_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = {
        "reserve_gib": 0,
        "jobs": [
            {
                "id": "writer",
                "command": ["python3", "-c", "print('x')"],
                "writes_database": True,
                "broad_scope": False,
            }
        ],
    }
    with pytest.raises(RuntimeError, match="auto-backfill-lock"):
        execute_plan(
            plan,
            repo_root=repo,
            state_path=tmp_path / "state.json",
            log_dir=tmp_path / "logs",
            executor_lock=tmp_path / "executor.lock",
            auto_backfill_lock=None,
            writer_lock=tmp_path / "writer.lock",
        )


def test_writer_runs_when_auto_lock_is_free(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = {
        "reserve_gib": 0,
        "jobs": [
            {
                "id": "writer",
                "command": [
                    "python3", "-c",
                    "from pathlib import Path; Path('written').write_text('ok')",
                ],
                "writes_database": True,
                "broad_scope": False,
            }
        ],
    }
    summary = execute_plan(
        plan,
        repo_root=repo,
        state_path=tmp_path / "state.json",
        log_dir=tmp_path / "logs",
        executor_lock=tmp_path / "executor.lock",
        auto_backfill_lock=tmp_path / "auto.lock",
        writer_lock=tmp_path / "writer.lock",
    )
    assert summary["completed"] == 1
    assert (repo / "written").read_text() == "ok"


def test_dry_run_allows_broad_writer_without_locks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = {
        "reserve_gib": 0,
        "jobs": [
            {
                "id": "broad-writer",
                "command": ["python3", "-c", "raise SystemExit(99)"],
                "writes_database": True,
                "broad_scope": True,
            }
        ],
    }
    summary = execute_plan(
        plan,
        repo_root=repo,
        state_path=tmp_path / "state.json",
        log_dir=tmp_path / "logs",
        executor_lock=tmp_path / "executor.lock",
        auto_backfill_lock=None,
        writer_lock=tmp_path / "writer.lock",
        dry_run=True,
    )
    assert summary["completed"] == 1
