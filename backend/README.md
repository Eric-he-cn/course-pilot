# CoursePilot 2.0 backend

Run from the project root after installing `backend/requirements.txt`:

```bash
uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

The service reads the project `.env` without exposing credentials. The
OpenAI-compatible adapter is active when `TEXT_BASE_URL`, `TEXT_API_KEY`,
`TEXT_MODEL` and `COURSEPILOT_ENABLE_REMOTE_LLM=1` are present — any provider
speaking Chat Completions works, and vendor-specific request fields go through
`TEXT_EXTRA_BODY`. Disabled, no-evidence and provider-error paths remain
available through explicit local fallbacks.
