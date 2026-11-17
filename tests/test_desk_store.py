from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scheme1.contracts import Fill, Side
from scheme1.desk.store import DeskStore

T = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def fill(i):
    return Fill(order_id=f"ord_{i}", symbol="solana:X", side=Side.BUY, quantity=Decimal(i + 1),
                price=Decimal("1.5"), reference_price=Decimal("1.5"), fee=Decimal("0.01"),
                candle_open_time=T, filled_at=T, liquidity="taker")


def filled_store(n=3):
    store = DeskStore()
    with store.transaction():
        for i in range(n):
            store.append("fill", f"fill_{i}", T, fill(i), symbol="solana:X")
        store.append("event", "ev_1", T, {"what": "test", "cash": Decimal("1000")})
    return store


def test_records_come_back_in_order_and_typed():
    store = filled_store()
    assert [f.quantity for f in store.fills()] == [1, 2, 3]
    event, = store.records("event")
    assert event["cash"] == "1000" and event["_seq"] == 4
    assert store.verify_chain() == (True, "4 records, chain intact")


def test_writes_need_a_transaction():
    store = DeskStore()
    with pytest.raises(RuntimeError):
        store.append("event", "e", T, {})
    with pytest.raises(RuntimeError):
        store.put_state("k", 1)


def test_a_failed_step_leaves_nothing_behind():
    store = filled_store()
    with pytest.raises(ZeroDivisionError):
        with store.transaction():
            store.append("event", "half", T, {"step": "half done"})
            store.put_state("flags", {"paused": "x"})
            1 / 0
    assert len(store.records()) == 4 and store.get_state("flags") is None
    assert store.verify_chain()[0]


def test_the_log_refuses_edits_and_deletes():
    store = filled_store()
    with pytest.raises(Exception, match="append-only"):
        store._db.execute("UPDATE audit_log SET payload = '{}' WHERE seq = 2")
    with pytest.raises(Exception, match="append-only"):
        store._db.execute("DELETE FROM audit_log WHERE seq = 2")


@pytest.mark.parametrize("tamper,message", [
    ("UPDATE audit_log SET payload = replace(payload, '\"1.5\"', '\"0.5\"') WHERE seq = 2", "record 2 (fill) was changed"),
    ("DELETE FROM audit_log WHERE seq = 2", "record 2 is missing"),
    ("UPDATE audit_log SET prev_hash = hash WHERE seq = 3", "record 3 doesn't follow"),
])
def test_tampering_is_detected(tamper, message):
    store = filled_store()
    store._db.executescript("DROP TRIGGER audit_log_no_update; DROP TRIGGER audit_log_no_delete;")
    store._db.execute(tamper)
    ok, why = store.verify_chain()
    assert not ok and message in why


def test_state_and_decision_details(tmp_path):
    path = tmp_path / "desk.db"
    store = DeskStore(path)
    with store.transaction():
        store.put_state("flags", {"paused": None, "n": 2})
    store.close()
    again = DeskStore(path)  # survives a restart
    assert again.get_state("flags") == {"paused": None, "n": 2} and again.get_state("missing", 7) == 7


def test_decision_details_are_purged_after_24_hours():
    from test_agents import inputs
    from scheme1.agents import run_agents
    from scheme1.decision import decide
    x = inputs()
    decision = decide(x, run_agents(x), x.features.as_of + timedelta(seconds=5), Decimal(1))
    store = DeskStore()
    with store.transaction():
        store.save_decision_details(decision)
    assert store.decision_details(decision.decision_id) == decision
    with store.transaction():
        assert store.purge_details(decision.created_at + timedelta(hours=23)) == 0
        assert store.purge_details(decision.created_at + timedelta(hours=25)) == 1
    assert store.decision_details(decision.decision_id) is None
