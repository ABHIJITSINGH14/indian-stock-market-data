import json
from unittest.mock import patch
from datetime import date

from market_data.auto_backfill import (
    AutoBackfillRunner,
    BackfillPhase,
    StateStore,
    phase_commands,
    retry_delay,
)
from scripts.auto_backfill import install


class FakeRunner(AutoBackfillRunner):
    def __init__(self, *args, free_bytes, exit_codes, **kwargs):
        super().__init__(*args, **kwargs)
        self.available_bytes = free_bytes
        self.exit_codes = dict(exit_codes)
        self.calls = []

    def free_bytes(self):
        return self.available_bytes

    def run_phase(self, phase):
        self.calls.append(phase.name)
        return self.exit_codes.get(phase.name, 0)


def test_phase_commands_cover_all_dataset_surfaces():
    phases = phase_commands(
        "/venv/python",
        "sqlite:////tmp/market.db",
        today=date(2026, 9, 19),
    )
    names = [phase.name for phase in phases]
    assert names == [
        "nse_prices",
        "bse_prices",
        "nse_fundamentals",
        "bse_fundamentals",
        "recent_disclosures",
        "institutional_activity",
    ]
    disclosure = phases[-2].command
    for dataset in ("corporate_actions", "board_meetings", "pit", "sast", "fii_dii"):
        assert dataset in disclosure
    assert "2026-08-05" in disclosure
    institutional = phases[-1].command
    assert "backfill_institutional.py" in institutional[1]
    assert "1999-01-01" in institutional


def test_phase_commands_include_periodic_local_manifest_catalog(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    raw_root = tmp_path / "raw"
    phases = phase_commands(
        "/venv/python",
        "sqlite:////tmp/market.db",
        today=date(2026, 9, 19),
        manifest_path=manifest,
        raw_root=raw_root,
    )
    local = phases[0]
    assert local.name == "local_manifest_catalog"
    assert local.success_interval == 6 * 60 * 60
    assert local.command[-1] == "--catalog-all"
    assert str(manifest) in local.command
    assert str(raw_root) in local.command
    deals = phases[1]
    assert deals.name == "official_market_deals"
    assert deals.success_interval == 6 * 60 * 60
    assert "import_market_deals.py" in deals.command[1]
    assert str(raw_root.parent / "deals_compact.db") in deals.command


def test_retry_delay_is_bounded_exponential():
    phase = BackfillPhase("test", ("true",), failure_interval=10)
    assert [retry_delay(phase, attempt) for attempt in range(1, 6)] == [
        10, 20, 40, 80, 80
    ]


def test_state_store_writes_atomically(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    assert store.load() == {"phases": {}}
    state = {"phases": {"nse_prices": {"next_run": 123}}}
    store.save(state)
    assert json.loads(path.read_text()) == state
    assert not path.with_suffix(".json.tmp").exists()


def test_runner_persists_success_and_failure_backoff(tmp_path):
    phases = [
        BackfillPhase("success", ("true",), success_interval=100),
        BackfillPhase("failure", ("false",), failure_interval=20),
    ]
    store = StateStore(tmp_path / "state.json")
    runner = FakeRunner(
        phases,
        store,
        tmp_path / "runner.lock",
        minimum_free_bytes=10,
        clock=lambda: 1000,
        sleeper=lambda _seconds: None,
        free_bytes=100,
        exit_codes={"failure": 1},
    )
    runner.run(once=True)

    assert runner.calls == ["success", "failure"]
    state = store.load()["phases"]
    assert state["success"]["next_run"] == 1100
    assert state["failure"]["next_run"] == 1020
    assert state["failure"]["failures"] == 1


def test_runner_defers_all_phases_when_disk_is_low(tmp_path):
    phase = BackfillPhase("prices", ("true",))
    store = StateStore(tmp_path / "state.json")
    runner = FakeRunner(
        [phase],
        store,
        tmp_path / "runner.lock",
        minimum_free_bytes=10,
        poll_interval=30,
        clock=lambda: 1000,
        sleeper=lambda _seconds: None,
        free_bytes=9,
        exit_codes={},
    )
    runner.run(once=True)

    assert runner.calls == []
    assert store.load()["phases"]["prices"]["last_status"] == "low_disk"
    assert store.load()["phases"]["prices"]["next_run"] == 1030


def test_runner_counts_reusable_sqlite_pages_as_effective_headroom(tmp_path):
    phase = BackfillPhase("prices", ("true",))
    runner = FakeRunner(
        [phase],
        StateStore(tmp_path / "state.json"),
        tmp_path / "runner.lock",
        minimum_free_bytes=12,
        minimum_physical_bytes=8,
        reusable_bytes=lambda: 3,
        clock=lambda: 1000,
        sleeper=lambda _seconds: None,
        free_bytes=9,
        exit_codes={},
    )
    runner.run(once=True)
    assert runner.calls == ["prices"]


def test_runner_never_uses_freelist_below_physical_floor(tmp_path):
    phase = BackfillPhase("prices", ("true",))
    store = StateStore(tmp_path / "state.json")
    runner = FakeRunner(
        [phase],
        store,
        tmp_path / "runner.lock",
        minimum_free_bytes=12,
        minimum_physical_bytes=8,
        reusable_bytes=lambda: 100,
        poll_interval=30,
        clock=lambda: 1000,
        sleeper=lambda _seconds: None,
        free_bytes=7,
        exit_codes={},
    )
    runner.run(once=True)
    assert runner.calls == []
    state = store.load()["phases"]["prices"]
    assert state["last_free_bytes"] == 7
    assert state["last_reusable_bytes"] == 100
    assert state["last_effective_bytes"] == 107


def test_install_reports_launchctl_bootstrap_failure(tmp_path, capsys):
    class Args:
        database_url = "sqlite:////tmp/market.db"
        state_file = tmp_path / "state.json"
        min_free_gib = 12.0
        manifest = tmp_path / "manifest.jsonl"
        raw_root = tmp_path / "raw"

    failed = __import__("subprocess").CompletedProcess(
        ["launchctl"], 5, stdout="", stderr="Bootstrap failed"
    )
    with patch("scripts.auto_backfill.Path.home", return_value=tmp_path), patch(
        "scripts.auto_backfill.subprocess.run",
        side_effect=[
            __import__("subprocess").CompletedProcess(["launchctl"], 0),
            __import__("subprocess").CompletedProcess(["launchctl"], 0),
            failed,
        ],
    ):
        assert install(Args()) == 5

    error = capsys.readouterr().err
    assert "LaunchAgent written" in error
    assert "Start it for this login" in error
