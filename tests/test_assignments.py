"""A dirty room is assigned, the clean is timed against the injected clock, the duration is stored once."""
from roomready.db import get_session
from roomready.models import Assignment
from tests.conftest import assign, assigned, checkout, room


def test_a_dirty_room_is_assigned_and_shows_assigned(client, clock):
    checkout(client, "214")
    r = assign(client, "214", "HK03")
    assert r.status_code == 201, r.text
    assert r.json() == {"assignment_id": "AS001", "room": "214", "assigned_to": "Sunita", "assigned_at": "2026-09-28T10:40:00Z"}
    assert room(client, "214")["state"] == "assigned" and room(client, "214")["assigned_to"] == "Sunita"


def test_start_then_finish_thirty_six_minutes_later_stores_thirty_six(client, clock, db):
    a = assigned(client, "214", "HK03")
    clock.advance(minutes=15)
    r = client.post(f"/assignments/{a['assignment_id']}/start")
    assert r.status_code == 200 and r.json() == {"assignment_id": "AS001", "started_at": "2026-09-28T10:55:00Z"}
    assert room(client, "214")["state"] == "cleaning"
    clock.advance(minutes=36)
    r = client.post(f"/assignments/{a['assignment_id']}/finish")
    assert r.status_code == 200
    assert r.json() == {"assignment_id": "AS001", "finished_at": "2026-09-28T11:31:00Z", "clean_minutes": 36}, "the shape: no state"
    assert room(client, "214")["state"] == "toinspect"
    clock.advance(hours=3)
    with db() as s:
        assert s.get(Assignment, 1).clean_minutes == 36, "stored at finish, not recomputed on read"
    assert room(client, "214")["since_minutes"] == 180


def test_clean_minutes_are_whole_minutes_rounded_down(client, clock):
    a = assigned(client, "214", "HK03")
    client.post(f"/assignments/{a['assignment_id']}/start")
    clock.advance(minutes=36, seconds=59)
    assert client.post(f"/assignments/{a['assignment_id']}/finish").json()["clean_minutes"] == 36


def test_finish_without_start_is_step_skipped_needing_start(client):
    a = assigned(client, "214", "HK03")
    r = client.post(f"/assignments/{a['assignment_id']}/finish")
    assert r.status_code == 409
    body = r.json()
    assert body["type"] == "/errors/step-skipped" and body["state"] == "assigned" and body["needs"] == "start"


def test_start_twice_and_finish_twice_are_step_skipped(client):
    a = assigned(client, "214", "HK03")
    assert client.post(f"/assignments/{a['assignment_id']}/start").status_code == 200
    r = client.post(f"/assignments/{a['assignment_id']}/start")
    assert r.status_code == 409 and r.json()["state"] == "cleaning" and r.json()["needs"] == "finish"
    assert client.post(f"/assignments/{a['assignment_id']}/finish").status_code == 200
    r = client.post(f"/assignments/{a['assignment_id']}/finish")
    assert r.status_code == 409 and r.json()["state"] == "toinspect" and r.json()["needs"] == "inspect"


def test_the_404s(client):
    checkout(client, "214")
    r = assign(client, "999", "HK03")
    assert r.status_code == 404 and r.json()["type"] == "/errors/no-such-room"
    r = assign(client, "214", "HK99")
    assert r.status_code == 404 and r.json()["type"] == "/errors/no-such-housekeeper"
    for action in ("start", "finish"):
        r = client.post(f"/assignments/AS999/{action}")
        assert r.status_code == 404 and r.json()["type"] == "/errors/no-such-assignment"


def test_the_clock_is_the_overridden_dependency(client, clock):
    """Every stamp comes from the clock the test injected through `dependency_overrides[get_clock]` (conftest)."""
    checkout(client, "214")
    assert assign(client, "214", "HK03").json()["assigned_at"] == "2026-09-28T10:40:00Z"
    clock.advance(hours=1, minutes=5)
    checkout(client, "215", at="2026-09-28T11:45:00Z")
    assert assign(client, "215", "HK04").json()["assigned_at"] == "2026-09-28T11:45:00Z", "the stamp moved with the injected clock"


def test_the_session_is_a_dependency_a_test_can_override(app, client, db):
    """The routes take their session from `Depends(get_session)`: an override sees every request's session."""
    used = []

    def spy_session():
        with db() as session:
            used.append(session)
            yield session

    app.dependency_overrides[get_session] = spy_session
    checkout(client, "214")
    assert assign(client, "214", "HK03").status_code == 201
    assert room(client, "214")["state"] == "assigned"
    assert len(used) == 3, "checkout, assign and the read each ran on the overridden session"
