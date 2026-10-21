from queue_watch import demo
from queue_watch.model import validate_snapshot


class Clock:
    now_value = 1_800_000_000.0

    def now(self) -> float:
        return self.now_value


def test_demo_queue_keeps_moving_with_valid_snapshots() -> None:
    clock = Clock()
    simulation = demo.Simulation(clock)
    lengths, merged = [], set()
    for _ in range(400):  # 400 ticks: well past every pattern's period
        clock.now_value += demo.STEP
        queue = validate_snapshot(simulation.merge_queue())
        validate_snapshot(simulation.ci())
        entries = [r for r in queue["records"] if r["kind"] == "queue_entry"]
        assert [e["position"] for e in entries] == list(range(1, len(entries) + 1))
        assert sum(e["raw_state"] == "merging" for e in entries) <= 1
        lengths.append(len(entries))
        merged |= {r["id"] for r in queue["records"] if r["kind"] == "attempt"}
    assert len(merged) > 100
    assert max(lengths) <= 8  # arrivals and merges balance; the queue never runs away
