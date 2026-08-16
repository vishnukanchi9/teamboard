# TeamBoard

Real-time team task manager with a kanban workflow, priorities, assignees,
comments, live browser updates through WebSockets, and an activity feed.

## Run it

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --reload
```

Open `http://localhost:8000`. The app creates a local SQLite database and demo
workspace on its first start.

## Portfolio highlights

- Designed REST endpoints for tasks and comments, with validation at the API boundary.
- Broadcast workspace changes over WebSockets so open browser tabs refresh in real time.
- Persisted boards, members, tasks, comments, and activity history with foreign-key constraints.
