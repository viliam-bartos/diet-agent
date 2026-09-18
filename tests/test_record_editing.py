"""Tests for editing and deleting individual records through the dashboard API."""

import shutil
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nutrition_agent.web as web_module
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.models import CalculatedFoodLog, CalculatedWeightLog, CalculatedWorkoutLog
from nutrition_agent.nutrition import recalculate_food_log
from nutrition_agent.repository import (
    delete_food_log,
    delete_weight_log,
    delete_workout_log,
    ensure_food_catalog,
    get_food_log,
    get_weight_log,
    get_workout_log,
    save_food_logs,
    save_weight_log,
    save_workout_log,
    update_food_log,
    update_weight_log,
    update_workout_log,
)

# Sandbox restricts the OS temp directory, so test databases live in the workspace.
TEST_TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp" / "tests"

TODAY = datetime.now().strftime("%Y-%m-%d")


@pytest.fixture
def record_db() -> tuple[object, dict[str, int]]:
    """Database with one food, weight and workout record, plus their ids."""
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    db_dir = TEST_TMP_ROOT / uuid.uuid4().hex
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "records.db"

    try:
        init_db(db_path)
        conn = get_connection(db_path)
        ensure_food_catalog(conn, web_module.settings.foods_data_path)

        food = save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{TODAY}T12:00:00Z",
                    raw_input="200g kuřecí",
                    food_name_matched="Kuřecí prsa",
                    amount_g=200.0,
                    state="raw",
                    is_estimated=False,
                    calories=212.0,
                    protein_raw=46.0,
                    protein_usable=46.0,
                    carbs=0.0,
                    fats=2.4,
                )
            ],
        )[0]
        weight = save_weight_log(
            conn,
            CalculatedWeightLog(logged_at=f"{TODAY}T07:00:00Z", raw_input="83", weight_kg=83.0),
        )
        workout = save_workout_log(
            conn,
            CalculatedWorkoutLog(
                logged_at=f"{TODAY}T18:00:00Z",
                raw_input="trenink",
                workout_type="Nohy",
                duration_minutes=60,
            ),
        )
        conn.commit()

        yield conn, {"food": food.id, "weight": weight.id, "workout": workout.id}
    finally:
        shutil.rmtree(db_dir, ignore_errors=True)


@pytest.fixture
def edit_client(record_db, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict]:
    """TestClient wired to the isolated database."""
    conn, ids = record_db
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    settings_override = type(web_module.settings)(
        database_path=db_path,
        foods_data_path=web_module.settings.foods_data_path,
        log_level="WARNING",
        gemini_api_key=None,
        discord_bot_token=None,
        discord_guild_id=None,
        web_host="127.0.0.1",
        web_port=8000,
        target_calories=None,
        target_protein_g=None,
    )
    monkeypatch.setattr(web_module, "settings", settings_override)
    return TestClient(web_module.create_app()), ids


def test_recalculate_food_log_stays_consistent():
    """Recomputation keeps calories and protein in step with the new amount."""
    from nutrition_agent.models import FoodDefinition

    food = FoodDefinition(
        name="Kuřecí prsa", calories=106.0, protein=23.0, diaas_coefficient=1.0, carbs=0.0, fats=1.2
    )
    existing = CalculatedFoodLog(
        id=1,
        raw_input="200g kuřecí",
        food_name_matched="Kuřecí prsa",
        amount_g=200.0,
        state="raw",
        is_estimated=False,
        calories=212.0,
        protein_raw=46.0,
        protein_usable=46.0,
        carbs=0.0,
        fats=2.4,
    )

    # 350 g cooked: 350 * 1.25 = 437.5 g raw equivalent -> 106 * 4.375 = 463.75
    updated = recalculate_food_log(existing, food, 350.0, "cooked")
    assert updated.amount_g == 350.0
    assert updated.state == "cooked"
    assert updated.calories == 463.8
    assert updated.protein_raw == 100.62
    assert updated.protein_usable == 100.62
    assert updated.fats == 5.25
    # Identity and provenance are preserved
    assert updated.id == 1
    assert updated.raw_input == "200g kuřecí"
    assert updated.is_estimated is False


def test_recalculate_rejects_non_positive_amount():
    """Zero grams is refused instead of producing an empty log."""
    from nutrition_agent.models import FoodDefinition

    food = FoodDefinition(name="X", calories=100.0, protein=1.0, carbs=1.0, fats=1.0)
    existing = CalculatedFoodLog(
        raw_input="x",
        food_name_matched="X",
        amount_g=100.0,
        state="raw",
        is_estimated=False,
        calories=100.0,
        protein_raw=1.0,
        protein_usable=1.0,
        carbs=1.0,
        fats=1.0,
    )

    with pytest.raises(ValueError, match="kladné číslo"):
        recalculate_food_log(existing, food, 0.0, "raw")


def test_update_food_log_persists_recomputed_values(record_db):
    """An edited food log is written back with its new macros."""
    conn, ids = record_db
    existing = get_food_log(conn, ids["food"])
    assert existing is not None

    from nutrition_agent.models import FoodDefinition

    food = FoodDefinition(
        name="Kuřecí prsa", calories=106.0, protein=23.0, diaas_coefficient=1.0, carbs=0.0, fats=1.2
    )
    updated = recalculate_food_log(existing, food, 100.0, "raw")
    update_food_log(conn, updated)

    stored = get_food_log(conn, ids["food"])
    assert stored is not None
    assert stored.amount_g == 100.0
    assert stored.calories == 106.0
    assert stored.protein_usable == 23.0


def test_update_weight_and_workout_logs(record_db):
    """Weight and workout edits are persisted."""
    conn, ids = record_db

    weight = get_weight_log(conn, ids["weight"])
    assert weight is not None
    update_weight_log(conn, weight.model_copy(update={"weight_kg": 82.4}))
    assert get_weight_log(conn, ids["weight"]).weight_kg == 82.4

    workout = get_workout_log(conn, ids["workout"])
    assert workout is not None
    update_workout_log(
        conn,
        CalculatedWorkoutLog(
            id=workout.id,
            logged_at=workout.logged_at,
            raw_input=workout.raw_input,
            workout_type="Nohy a břicho",
            duration_minutes=75,
            notes="těžké",
        ),
    )
    stored = get_workout_log(conn, ids["workout"])
    assert stored.workout_type == "Nohy a břicho"
    assert stored.duration_minutes == 75
    assert stored.notes == "těžké"


def test_update_missing_record_raises(record_db):
    """Updating a row that does not exist raises LookupError."""
    conn, _ = record_db
    ghost = CalculatedWeightLog(id=99999, raw_input="x", weight_kg=80.0)

    with pytest.raises(LookupError, match="nebyl nalezen"):
        update_weight_log(conn, ghost)

    with pytest.raises(ValueError, match="musí mít záznam své id"):
        update_weight_log(conn, CalculatedWeightLog(raw_input="x", weight_kg=80.0))


def test_delete_single_records(record_db):
    """Each record type can be deleted by id."""
    conn, ids = record_db

    assert delete_food_log(conn, ids["food"]) is True
    assert delete_food_log(conn, ids["food"]) is False
    assert delete_weight_log(conn, ids["weight"]) is True
    assert delete_workout_log(conn, ids["workout"]) is True

    assert get_food_log(conn, ids["food"]) is None
    assert get_weight_log(conn, ids["weight"]) is None
    assert get_workout_log(conn, ids["workout"]) is None


def test_api_patch_food_recomputes_and_persists(edit_client):
    """PATCH on a food record recomputes macros server side."""
    client, ids = edit_client

    response = client.patch(
        f"/api/records/food/{ids['food']}",
        json={"amount_g": 350.0, "state": "cooked"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["amount_g"] == 350.0
    assert body["state"] == "cooked"
    assert body["calories"] == 463.8
    assert body["protein_usable"] == 100.62

    listed = client.get("/api/records", params={"type": "food"}).json()["items"][0]
    assert listed["amount_g"] == 350.0
    assert "350 g" in listed["title"]


def test_api_patch_weight_and_workout(edit_client):
    """PATCH works for weight and workout records too."""
    client, ids = edit_client

    weight = client.patch(f"/api/records/weight/{ids['weight']}", json={"weight_kg": 82.4})
    assert weight.status_code == 200
    assert weight.json()["weight_kg"] == 82.4
    assert (
        "82.4 kg"
        in client.get("/api/records", params={"type": "weight"}).json()["items"][0]["title"]
    )

    workout = client.patch(
        f"/api/records/workout/{ids['workout']}",
        json={"workout_type": "Nohy a břicho", "duration_minutes": 75, "notes": "těžké"},
    )
    assert workout.status_code == 200
    assert workout.json()["duration_minutes"] == 75

    item = client.get("/api/records", params={"type": "workout"}).json()["items"][0]
    assert "Nohy a břicho" in item["title"]
    assert item["notes"] == "těžké"


def test_api_patch_workout_without_duration(edit_client):
    """An empty duration is stored as NULL, not as zero."""
    client, ids = edit_client

    response = client.patch(
        f"/api/records/workout/{ids['workout']}",
        json={"workout_type": "Procházka", "duration_minutes": None, "notes": None},
    )
    assert response.status_code == 200
    assert response.json()["duration_minutes"] is None


@pytest.mark.parametrize(
    ("record_type", "payload"),
    [
        ("food", {"amount_g": 0}),
        ("food", {"amount_g": -10}),
        ("food", {"amount_g": "hodně"}),
        ("weight", {"weight_kg": 500}),
        ("weight", {"weight_kg": 5}),
        ("workout", {"workout_type": "", "duration_minutes": 60}),
        ("workout", {"workout_type": "Běh", "duration_minutes": 0}),
        ("workout", {"workout_type": "Běh", "duration_minutes": 5000}),
    ],
)
def test_api_patch_rejects_invalid_values(edit_client, record_type: str, payload: dict):
    """Invalid edits return 422 with a readable message."""
    client, ids = edit_client

    response = client.patch(f"/api/records/{record_type}/{ids[record_type]}", json=payload)
    assert response.status_code == 422
    assert "Neplatná hodnota pole" in response.json()["detail"]


def test_api_patch_unknown_id_returns_404(edit_client):
    """Editing a missing record reports 404."""
    client, _ = edit_client
    assert client.patch("/api/records/food/99999", json={"amount_g": 100}).status_code == 404
    assert client.patch("/api/records/weight/99999", json={"weight_kg": 80}).status_code == 404
    assert (
        client.patch("/api/records/workout/99999", json={"workout_type": "Běh"}).status_code == 404
    )


def test_api_patch_rejects_unknown_record_type(edit_client):
    """An unsupported record type is rejected by path validation."""
    client, _ = edit_client
    assert client.patch("/api/records/nesmysl/1", json={"amount_g": 10}).status_code == 422


def test_api_delete_record(edit_client):
    """DELETE removes exactly one record and is idempotent-safe."""
    client, ids = edit_client

    assert client.delete(f"/api/records/workout/{ids['workout']}").status_code == 200
    assert client.delete(f"/api/records/workout/{ids['workout']}").status_code == 404

    remaining = client.get("/api/records", params={"type": "all"}).json()
    assert remaining["total"] == 2
    assert all(item["type"] != "workout" for item in remaining["items"])


def test_api_delete_unknown_record_type(edit_client):
    """Deleting with an unsupported type is rejected."""
    client, _ = edit_client
    assert client.delete("/api/records/nesmysl/1").status_code == 422


def test_api_patch_food_fails_when_food_left_catalog(edit_client, record_db):
    """A log whose food vanished from the catalog cannot be recomputed."""
    client, ids = edit_client
    conn, _ = record_db

    conn.execute("DELETE FROM foods WHERE name_key = 'kuřecí prsa'")
    conn.commit()

    response = client.patch(f"/api/records/food/{ids['food']}", json={"amount_g": 150.0})
    assert response.status_code == 409
    assert "už není v katalogu" in response.json()["detail"]


def test_api_records_expose_editable_fields(edit_client):
    """The records payload carries the fields the edit form needs."""
    client, _ = edit_client
    items = client.get("/api/records", params={"type": "all"}).json()["items"]

    by_type = {item["type"]: item for item in items}

    assert by_type["food"]["amount_g"] == 200.0
    assert by_type["food"]["state"] == "raw"
    assert by_type["weight"]["weight_kg"] == 83.0
    assert by_type["workout"]["workout_type"] == "Nohy"
    assert by_type["workout"]["duration_minutes"] == 60
    assert "notes" in by_type["workout"]
