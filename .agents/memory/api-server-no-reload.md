---
name: api-server dev has no hot-reload
description: Editing Python in artifacts/api-server does NOT take effect until the workflow is restarted — there is no uvicorn --reload.
---

The `artifacts/api-server` dev workflow runs uvicorn under a watchdog supervisor
WITHOUT `--reload`. Editing a `.py` file does **not** change the behavior of the
already-running server.

**Why:** This silently burned debugging time — a fix was correct in the file
(and verified via a standalone `python -c` script that imports the fresh module),
yet the live executor/poller kept exhibiting the OLD behavior because the running
process had loaded the pre-edit modules. Comparing file mtime vs. the uvicorn
process start time (`ps -eo pid,lstart …`) is the fastest way to confirm staleness.

**How to apply:** After ANY edit to api-server Python that you intend to verify
through the running server (poller drain, executor, lifecycle, resolver), call
`restart_workflow("artifacts/api-server: API Server")` BEFORE re-testing live.
A standalone import test proves the file is right but NOT that the server runs it.
