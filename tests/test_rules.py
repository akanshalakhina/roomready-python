"""One parametrised table of every refusal, run with the clock overridden under each INSPECTION_REQUIRED_FOR
setting — and only the rows tagged `inspection` move when the setting changes."""
import csv
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roomready.app import create_app
from roomready.clock import get_clock
from tests.conftest import T0, iso, make_settings

ROWS = list(csv.DictReader((Path(__file__).parent / "rules.csv").open(encoding="utf-8")))
# Deluxe (room 214, the room every generic row uses) needs inspection under all three, so only the two tagged rows can move
SETTINGS = {"all": "Standard,Deluxe,Suite", "deluxe_suite": "Deluxe,Suite", "standard_deluxe": "Standard,Deluxe"}


def step(client, text, last):
    verb, *args = text.split()
    if verb == "checkout":
        last["room"] = args[0]
        return client.post("/checkouts", json={"room": args[0], "at": iso(T0)})
    if verb == "checkout_without_time":
        return client.post("/checkouts", json={"room": args[0]})
    if verb == "assign":
        r = client.post("/assignments", json={"room": args[0], "housekeeper": args[1]})
        if r.status_code == 201:
            last["assignment"] = r.json()["assignment_id"]
        return r
    if verb in ("start", "finish"):
        return client.post(f"/assignments/{last['assignment']}/{verb}")
    if verb == "inspect":
        return client.post("/inspections", json={"room": last["room"], "inspector": args[0], "result": args[1]})
    raise ValueError(text)



def run_case(client, row):
    last = {}
    for text in filter(None, row["setup"].split(";")):
        r = step(client, text, last)
        assert r.status_code in (200, 201), (text, r.text)
    return step(client, row["action"], last)


@pytest.mark.parametrize("mode", SETTINGS)
@pytest.mark.parametrize("row", ROWS, ids=[r["case"] for r in ROWS])
def test_rule_table(tmp_path, clock, row, mode):
    app = create_app(settings=make_settings(tmp_path, inspection_required_for=SETTINGS[mode]))
    app.dependency_overrides[get_clock] = lambda: clock
    with TestClient(app) as client:
        r = run_case(client, row)
    assert r.status_code == int(row[f"status_{mode}"]), (row["case"], mode, r.text)
    expected_type = row[f"type_{mode}"]
    if expected_type:
        assert r.json()["type"] == expected_type
    if row["tag"] == "inspection" and expected_type:
        assert r.json()["state"] == "ready" and r.json()["needs"] == "checkin", ("finish already made it ready; a guest checks in next")
