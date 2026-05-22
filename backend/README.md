# Backend

This backend is intentionally dependency-free. It uses Python's standard library to serve:

- static frontend files
- `/api/state`
- workflow mutation endpoints
- Markdown export

The current engine is a deterministic Fake Agent Pipeline. It does not call any LLM.

## Run

```powershell
python backend/server.py
```

Open:

```text
http://127.0.0.1:5180/
```
