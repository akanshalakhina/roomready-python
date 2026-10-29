"""Every transition writes an event in the same transaction, with who did it; events can never be updated or deleted."""
from unittest.mock import patch

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from roomready.models import Event, Stay
from tests.conftest import ROOT, T0, P, checkout, cleaned, inspect, iso

PKG = ROOT / "roomready"


def test_a_pass_commits_exactly_one_inspected_event_with_inspector_and_result(client, clock, db):
    cleaned(client, clock, "214", "HK03", arrival_at="2026-09-28T13:00:00Z")
    clock.advance(minutes=64)
    inspect(client, "214", "HK01", "pass")
    with db() as s:
        rows = s.scalars(select(Event).where(Event.kind == "inspected")).all()
        assert len(rows) == 1 and rows[0].actor_id == "HK01" and rows[0].assignment_id == 1
        assert '"inspector": "HK01"' in rows[0].data and '"result": "pass"' in rows[0].data and '"margin_minutes": 40' in rows[0].data


def test_a_failed_transaction_leaves_no_event(client, clock, db):
    cleaned(client, clock, "214", "HK03")
    with db() as s:
        before = s.scalar(select(func.count()).select_from(Event))
    with patch("roomready.services.inspections.events.record", side_effect=RuntimeError("boom")):
        assert inspect(client, "214", "HK01", "pass").status_code == 500
    with db() as s:
        assert s.scalar(select(func.count()).select_from(Event)) == before


def test_events_are_append_only(client, db):
    checkout(client, "214")
    with db() as s, pytest.raises(DBAPIError):
        s.execute(update(Event).values(kind="edited"))
        s.commit()
    with db() as s, pytest.raises(DBAPIError):
        s.execute(delete(Event))
        s.commit()
    with db() as s, pytest.raises(DBAPIError):
        s.execute(text("DELETE FROM events WHERE id = 1"))
        s.commit()
    with db() as s:
        assert s.scalar(select(func.count()).select_from(Event)) == 1


def test_one_rooms_day_in_order_with_who_did_each_step(client, clock):
    checkout(client, "214", at=iso(T0), arrival_at="2026-09-28T13:00:00Z")   # 10:40
    clock.advance(minutes=12)
    a = client.post("/assignments", json={"room": "214", "housekeeper": "HK03"}).json()   # 10:52
    clock.advance(minutes=3)
    client.post(f"/assignments/{a['assignment_id']}/start")                                # 10:55
    clock.advance(minutes=36)
    client.post(f"/assignments/{a['assignment_id']}/finish")                               # 11:31
    clock.advance(minutes=49)
    inspect(client, "214", "HK01", "pass")                                                 # 12:20
    body = client.get(f"{P}/rooms/214/history").json()
    assert body["room"] == "214"
    assert body["events"] == [
        {"at": "2026-09-28T10:40:00Z", "kind": "checked_out"},
        {"at": "2026-09-28T10:52:00Z", "kind": "assigned", "housekeeper": "HK03"},
        {"at": "2026-09-28T10:55:00Z", "kind": "started"},
        {"at": "2026-09-28T11:31:00Z", "kind": "finished", "clean_minutes": 36},
        {"at": "2026-09-28T12:20:00Z", "kind": "inspected", "inspector": "HK01", "result": "pass", "margin_minutes": 40,
         "arrival_at": "2026-09-28T13:00:00Z"},
    ], "the read shape: no actor on the wire, no null keys; the pass carries the arrival its margin was measured against"


def test_who_did_it_is_stored_on_every_event_but_not_on_the_wire(client, clock, db):
    cleaned(client, clock, "214", "HK03")
    inspect(client, "214", "HK01", "fail")
    with db() as s:
        actors = [(e.kind, e.actor_id) for e in s.scalars(select(Event).order_by(Event.id))]
    assert actors == [("checked_out", "front-desk"), ("assigned", "supervisor"), ("started", "HK03"), ("finished", "HK03"),
                      ("inspected", "HK01")]
    events = client.get(f"{P}/rooms/214/history").json()["events"]
    assert all("actor" not in e and "actor_id" not in e for e in events)
    assert events[-1] == {"at": "2026-09-28T11:16:00Z", "kind": "inspected", "inspector": "HK01", "result": "fail"}


def test_events_are_inserted_never_loaded_to_change():
    """The event writer is one Core INSERT; nothing in the services adds, updates or deletes an Event object."""
    src = (PKG / "services" / "events.py").read_text(encoding="utf-8")
    assert "insert(Event)" in src and "session.add" not in src
    for path in (PKG / "services").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "update(Event" not in text and "delete(Event" not in text, path.name


def test_history_is_per_stay_and_defaults_to_the_current_one(client, clock, db):
    cleaned(client, clock, "214", "HK03")
    inspect(client, "214", "HK01", "pass")
    with db() as s:                                                   # the next guest arrives and leaves
        s.add(Stay(property_id=1, room_id=29, guest_ref="BK-214-2", checked_in_at=clock.now()))
        s.commit()
    clock.advance(days=1)
    checkout(client, "214", at=iso(clock.now()))
    current = client.get(f"{P}/rooms/214/history").json()["events"]
    assert [e["kind"] for e in current] == ["checked_out"]
    first = client.get(f"{P}/rooms/214/history?stay=ST0029").json()["events"]
    assert [e["kind"] for e in first] == ["checked_out", "assigned", "started", "finished", "inspected"]
    checkout(client, "101", at=iso(clock.now()))
    assert client.get(f"{P}/rooms/214/history?stay=ST0001").json()["events"] == [], "another room's stay shows nothing here"
    assert client.get(f"{P}/rooms/214/history?stay=ST9999").json()["events"] == []


def test_no_event_and_no_history_line_carries_the_guest(client, clock, db):
    cleaned(client, clock, "214", "HK03")
    inspect(client, "214", "HK01", "pass")
    with db() as s:
        guest = s.get(Stay, 29).guest_ref
        assert guest and all(guest not in e.data for e in s.scalars(select(Event)))
    assert guest not in client.get(f"{P}/rooms/214/history").text
