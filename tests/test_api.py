from fastapi.testclient import TestClient

from app import main


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE", tmp_path / "teamboard-test.db")
    main.connections.clear()
    main.initialize()
    return TestClient(main.app)


def test_task_moves_between_columns(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as api:
        workspace = api.get("/api/workspace").json()
        task = next(item for item in workspace["tasks"] if item["status"] == "todo")

        response = api.patch(f"/api/tasks/{task['id']}", json={"status": "doing"})

        assert response.status_code == 200
        updated = api.get("/api/workspace").json()
        moved = next(item for item in updated["tasks"] if item["id"] == task["id"])
        assert moved["status"] == "doing"


def test_member_can_be_added_edited_and_removed(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as api:
        created = api.post("/api/members", json={"name": "Avery Jones", "initials": "aj", "role": "Engineering"})
        member_id = created.json()["id"]
        assert created.status_code == 201

        edited = api.patch(f"/api/members/{member_id}", json={"name": "Avery Jones", "initials": "AJ", "role": "Platform"})
        assert edited.status_code == 200
        member = next(item for item in api.get("/api/workspace").json()["members"] if item["id"] == member_id)
        assert member["role"] == "Platform"

        removed = api.delete(f"/api/members/{member_id}")
        assert removed.status_code == 204
        assert all(item["id"] != member_id for item in api.get("/api/workspace").json()["members"])


def test_profile_updates_and_health_is_available(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as api:
        response = api.patch("/api/profile", json={"name": "Vishnu Kanchi", "initials": "VK", "role": "Software Engineer"})
        assert response.status_code == 200
        assert api.get("/api/workspace").json()["profile"]["role"] == "Software Engineer"
        assert api.get("/healthz").json() == {"status": "ok"}


def test_invalid_task_status_is_rejected(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as api:
        task_id = api.get("/api/workspace").json()["tasks"][0]["id"]
        response = api.patch(f"/api/tasks/{task_id}", json={"status": "archived"})
        assert response.status_code == 422
