"""Tests for the live-update channel.

This is the feature TeamBoard is actually about — a shared board that updates for
everyone without a refresh — and until now nothing exercised it. The REST tests
would all pass with the WebSocket endpoint deleted.
"""

from fastapi.testclient import TestClient

from app import main


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE", tmp_path / "teamboard-ws-test.db")
    main.connections.clear()
    main.initialize()
    return TestClient(main.app)


def test_moving_a_task_notifies_a_connected_client(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as api:
        with api.websocket_connect("/ws") as socket:
            task = next(
                item
                for item in api.get("/api/workspace").json()["tasks"]
                if item["status"] == "todo"
            )

            assert api.patch(f"/api/tasks/{task['id']}", json={"status": "doing"}).status_code == 200

            assert socket.receive_json() == {"type": "workspace.updated"}


def test_every_mutating_endpoint_broadcasts(tmp_path, monkeypatch):
    """A change that reaches the database but not the socket leaves other boards stale.

    Each mutation below must produce exactly one notification, so the assertions run one
    at a time rather than draining a batch at the end.
    """
    with client(tmp_path, monkeypatch) as api:
        with api.websocket_connect("/ws") as socket:
            created = api.post(
                "/api/members",
                json={"name": "Dana Reed", "initials": "DR", "role": "Engineering"},
            )
            assert created.status_code == 201
            assert socket.receive_json()["type"] == "workspace.updated"

            member_id = created.json()["id"]
            assert api.patch(
                f"/api/members/{member_id}",
                json={"name": "Dana Reed", "initials": "DR", "role": "Design"},
            ).status_code == 200
            assert socket.receive_json()["type"] == "workspace.updated"

            assert api.delete(f"/api/members/{member_id}").status_code == 204
            assert socket.receive_json()["type"] == "workspace.updated"


def test_two_clients_both_receive_the_same_update(tmp_path, monkeypatch):
    """The point of a shared board: one person's change reaches everyone else."""
    with client(tmp_path, monkeypatch) as api:
        with api.websocket_connect("/ws") as first, api.websocket_connect("/ws") as second:
            assert len(main.connections) == 2

            task = api.get("/api/workspace").json()["tasks"][0]
            api.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

            assert first.receive_json() == {"type": "workspace.updated"}
            assert second.receive_json() == {"type": "workspace.updated"}


def test_a_rejected_change_broadcasts_nothing(tmp_path, monkeypatch):
    """Broadcasting on a failed write would make every other board show a change
    that never happened."""
    with client(tmp_path, monkeypatch) as api:
        with api.websocket_connect("/ws") as socket:
            task = api.get("/api/workspace").json()["tasks"][0]
            assert api.patch(
                f"/api/tasks/{task['id']}", json={"status": "archived"}
            ).status_code == 422

            # Now make a change that *is* valid. If the rejected one had broadcast, this
            # would read that stale notification first and the ordering would prove it.
            api.patch(f"/api/tasks/{task['id']}", json={"status": "doing"})
            assert socket.receive_json() == {"type": "workspace.updated"}

            refreshed = next(
                item
                for item in api.get("/api/workspace").json()["tasks"]
                if item["id"] == task["id"]
            )
            assert refreshed["status"] == "doing"


def test_disconnecting_removes_the_connection(tmp_path, monkeypatch):
    """Without cleanup the set grows forever and every broadcast walks dead sockets."""
    with client(tmp_path, monkeypatch) as api:
        with api.websocket_connect("/ws"):
            assert len(main.connections) == 1

        # Closing the context manager disconnects; the endpoint's handler removes it.
        task = api.get("/api/workspace").json()["tasks"][0]
        api.patch(f"/api/tasks/{task['id']}", json={"status": "doing"})
        assert main.connections == set()
