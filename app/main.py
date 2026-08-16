import sqlite3
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATABASE = Path(os.getenv("TEAMBOARD_DATABASE", ROOT / "teamboard.db"))
STATIC = Path(__file__).parent / "static"
connections: set[WebSocket] = set()

app = FastAPI(title="TeamBoard", version="1.0.0", description="Real-time team task manager.")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def initialize() -> None:
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS boards (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, initials TEXT NOT NULL, role TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_profile (
                id INTEGER PRIMARY KEY CHECK(id = 1), name TEXT NOT NULL, initials TEXT NOT NULL, role TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL REFERENCES boards(id), title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN ('todo','doing','done')),
                priority TEXT NOT NULL CHECK(priority IN ('low','medium','high')), assignee_id INTEGER REFERENCES members(id),
                due_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY, message TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        if db.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0:
            created = now()
            db.execute("INSERT INTO boards(name, created_at) VALUES (?, ?)", ("Product launch", created))
            db.executemany(
                "INSERT INTO members(name, initials, role) VALUES (?, ?, ?)",
                [("Ava Chen", "AC", "Product"), ("Noah Williams", "NW", "Engineering"), ("Mia Patel", "MP", "Design")],
            )
            db.executemany(
                """INSERT INTO tasks(board_id,title,description,status,priority,assignee_id,due_date,created_at,updated_at)
                   VALUES (1,?,?,?,?,?,?,?,?)""",
                [
                    ("Confirm onboarding copy", "Review first-run language with support.", "todo", "medium", 1, "2026-08-22", created, created),
                    ("Build activity feed", "Show task events and comments in the workspace.", "doing", "high", 2, "2026-08-20", created, created),
                    ("Publish visual system", "Package the final component styles.", "done", "low", 3, "2026-08-18", created, created),
                ],
            )
            db.execute("INSERT INTO activity(message, created_at) VALUES (?, ?)", ("Created Product launch workspace", created))
        if db.execute("SELECT COUNT(*) FROM workspace_profile").fetchone()[0] == 0:
            db.execute(
                "INSERT INTO workspace_profile(id, name, initials, role) VALUES (1, ?, ?, ?)",
                ("Vishnu Kanchi", "VK", "Workspace owner"),
            )


class TaskCreate(BaseModel):
    board_id: int
    title: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=500)
    priority: str = "medium"
    assignee_id: int | None = None
    due_date: str | None = None


class TaskUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    assignee_id: int | None = None


class CommentCreate(BaseModel):
    author: str = Field(min_length=2, max_length=60)
    body: str = Field(min_length=1, max_length=500)


class MemberCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    initials: str = Field(min_length=1, max_length=3)
    role: str = Field(min_length=2, max_length=60)


class ProfileUpdate(MemberCreate):
    pass


async def broadcast() -> None:
    stale: list[WebSocket] = []
    for connection in connections:
        try:
            await connection.send_json({"type": "workspace.updated"})
        except Exception:
            stale.append(connection)
    for connection in stale:
        connections.discard(connection)


@app.on_event("startup")
def startup() -> None:
    initialize()


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/workspace")
def workspace() -> dict:
    with database() as db:
        return {
            "boards": rows(db.execute("SELECT * FROM boards ORDER BY id")),
            "members": rows(db.execute("SELECT * FROM members ORDER BY name")),
            "profile": dict(db.execute("SELECT name, initials, role FROM workspace_profile WHERE id = 1").fetchone()),
            "tasks": rows(
                db.execute(
                    """SELECT tasks.*, members.name AS assignee_name, members.initials AS assignee_initials
                       FROM tasks LEFT JOIN members ON members.id = tasks.assignee_id
                       ORDER BY CASE tasks.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, tasks.updated_at DESC"""
                )
            ),
            "activity": rows(db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 8")),
        }


@app.post("/api/members", status_code=201)
async def create_member(payload: MemberCreate) -> dict:
    with database() as db:
        cursor = db.execute(
            "INSERT INTO members(name, initials, role) VALUES (?, ?, ?)",
            (payload.name, payload.initials.upper(), payload.role),
        )
        db.execute("INSERT INTO activity(message, created_at) VALUES (?, ?)", (f"Added {payload.name} to the workspace", now()))
    await broadcast()
    return {"id": cursor.lastrowid}


@app.patch("/api/members/{member_id}")
async def update_member(member_id: int, payload: MemberCreate) -> dict:
    with database() as db:
        if not db.execute("SELECT 1 FROM members WHERE id = ?", (member_id,)).fetchone():
            raise HTTPException(404, "Member not found.")
        db.execute(
            "UPDATE members SET name = ?, initials = ?, role = ? WHERE id = ?",
            (payload.name, payload.initials.upper(), payload.role, member_id),
        )
        db.execute("INSERT INTO activity(message, created_at) VALUES (?, ?)", (f"Updated {payload.name}'s member profile", now()))
    await broadcast()
    return {"updated": True}


@app.delete("/api/members/{member_id}", status_code=204)
async def delete_member(member_id: int) -> None:
    with database() as db:
        member = db.execute("SELECT name FROM members WHERE id = ?", (member_id,)).fetchone()
        if not member:
            raise HTTPException(404, "Member not found.")
        db.execute("UPDATE tasks SET assignee_id = NULL WHERE assignee_id = ?", (member_id,))
        db.execute("DELETE FROM members WHERE id = ?", (member_id,))
        db.execute("INSERT INTO activity(message, created_at) VALUES (?, ?)", (f"Removed {member['name']} from the workspace", now()))
    await broadcast()


@app.patch("/api/profile")
async def update_profile(payload: ProfileUpdate) -> dict:
    with database() as db:
        db.execute(
            "UPDATE workspace_profile SET name = ?, initials = ?, role = ? WHERE id = 1",
            (payload.name, payload.initials.upper(), payload.role),
        )
        db.execute("INSERT INTO activity(message, created_at) VALUES (?, ?)", ("Updated workspace profile", now()))
    await broadcast()
    return {"updated": True}


@app.get("/api/tasks/{task_id}/comments")
def list_comments(task_id: int) -> list[dict]:
    with database() as db:
        return rows(db.execute("SELECT * FROM comments WHERE task_id = ? ORDER BY id", (task_id,)))


@app.post("/api/tasks", status_code=201)
async def create_task(payload: TaskCreate) -> dict:
    if payload.priority not in {"low", "medium", "high"}:
        raise HTTPException(422, "Priority must be low, medium, or high.")
    timestamp = now()
    with database() as db:
        if not db.execute("SELECT 1 FROM boards WHERE id = ?", (payload.board_id,)).fetchone():
            raise HTTPException(404, "Board not found.")
        cursor = db.execute(
            """INSERT INTO tasks(board_id,title,description,status,priority,assignee_id,due_date,created_at,updated_at)
               VALUES (?, ?, ?, 'todo', ?, ?, ?, ?, ?)""",
            (payload.board_id, payload.title, payload.description, payload.priority, payload.assignee_id, payload.due_date, timestamp, timestamp),
        )
        db.execute("INSERT INTO activity(message, created_at) VALUES (?, ?)", (f"Created task: {payload.title}", timestamp))
        task_id = cursor.lastrowid
    await broadcast()
    return {"id": task_id}


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: int, payload: TaskUpdate) -> dict:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "Include at least one field to update.")
    if "status" in changes and changes["status"] not in {"todo", "doing", "done"}:
        raise HTTPException(422, "Invalid task status.")
    if "priority" in changes and changes["priority"] not in {"low", "medium", "high"}:
        raise HTTPException(422, "Invalid priority.")
    with database() as db:
        task = db.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found.")
        assignments = [f"{column} = ?" for column in changes]
        values = [*changes.values(), now(), task_id]
        db.execute(f"UPDATE tasks SET {', '.join(assignments)}, updated_at = ? WHERE id = ?", values)
        db.execute("INSERT INTO activity(message, created_at) VALUES (?, ?)", (f"Updated task: {task['title']}", now()))
    await broadcast()
    return {"updated": True}


@app.post("/api/tasks/{task_id}/comments", status_code=201)
async def create_comment(task_id: int, payload: CommentCreate) -> dict:
    timestamp = now()
    with database() as db:
        task = db.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found.")
        cursor = db.execute("INSERT INTO comments(task_id,author,body,created_at) VALUES (?, ?, ?, ?)", (task_id, payload.author, payload.body, timestamp))
        db.execute("INSERT INTO activity(message, created_at) VALUES (?, ?)", (f"{payload.author} commented on {task['title']}", timestamp))
    await broadcast()
    return {"id": cursor.lastrowid}


@app.websocket("/ws")
async def websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.discard(websocket)
