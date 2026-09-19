import json
from datetime import date

from market_data.auto_backfill import (
    AutoBackfillRunner,
    BackfillPhase,
    StateStore,
    phase_commands,
    retry_delay,
)


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
    ]
    disclosure = phases[-1].command
    for dataset in ("corporate_actions", "board_meetings", "pit", "sast", "fii_dii"):
        assert dataset in disclosure
    assert "2026-08-05" in disclosure


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
