"""Smoke coverage for the detach + status modes of import_v1_history.

The detach mode forks the script into a long-running background process so
the 4.7M-row ``line_snapshots`` import survives the runner's 120s
foreground budget without requiring ~10 manual re-runs (task #819).  We
test the detach plumbing against a stub Python child that just writes
progress JSON and exits, so the test stays hermetic (no Supabase, no
psql).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import import_v1_history as mod


def test_progress_file_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "WORK_DIR", tmp_path)
    mod._write_progress("line_snapshots", chunks_completed=3, rows_loaded=1500000)
    mod._write_progress("line_snapshots", chunks_completed=4, rows_loaded=2000000)
    payload = json.loads((tmp_path / "line_snapshots.progress.json").read_text())
    assert payload["chunks_completed"] == 4
    assert payload["rows_loaded"] == 2000000
    assert "updated_at" in payload


def test_status_reports_alive_and_dead_processes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "WORK_DIR", tmp_path)
    mod._write_progress("line_snapshots", chunks_completed=2, rows_loaded=1000000)

    # Spawn a tiny sleeper as the "detached" process and record its PID.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        (tmp_path / "line_snapshots.detached.pid").write_text(f"{proc.pid}\n")
        (tmp_path / "line_snapshots.detached.log").write_text("hello\n")

        mod._print_status("line_snapshots")
        snapshot = json.loads(capsys.readouterr().out)
        assert snapshot["detached_pid"] == proc.pid
        assert snapshot["detached_alive"] is True
        assert snapshot["progress"]["chunks_completed"] == 2
        assert snapshot["log_size_bytes"] == len("hello\n")
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # After the child exits, the same status read should mark it dead.
    mod._print_status("line_snapshots")
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["detached_pid"] == proc.pid
    assert snapshot["detached_alive"] is False


def test_spawn_detached_forks_independent_child(tmp_path, monkeypatch):
    """Verify --detach actually re-execs a child via Popen+start_new_session.

    We monkeypatch the re-exec target by intercepting ``subprocess.Popen``
    so the stub child is a trivial ``python -c`` that writes a marker
    file and exits.  Tests both PID-file creation and detach idempotency
    (a second spawn while the first is alive should SystemExit).
    """
    monkeypatch.setattr(mod, "WORK_DIR", tmp_path)
    marker = tmp_path / "child-ran.marker"

    real_popen = subprocess.Popen
    captured = {}

    def fake_popen(cmd, **kwargs):
        # Replace the import_v1_history re-exec with a stub child that
        # writes the marker file and stays alive long enough to be
        # detected by the idempotency check.
        stub = [
            sys.executable, "-c",
            f"import time, pathlib; "
            f"pathlib.Path({str(marker)!r}).write_text('ok'); "
            f"time.sleep(2)",
        ]
        captured["cmd"] = cmd
        captured["start_new_session"] = kwargs.get("start_new_session")
        return real_popen(stub, **{k: v for k, v in kwargs.items() if k != "cwd"})

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = mod._spawn_detached(
        "line_snapshots", ["--table", "line_snapshots", "--chunk-rows", "500000"]
    )
    assert captured["start_new_session"] is True
    assert "import_v1_history" in " ".join(captured["cmd"])
    assert "--detach" not in captured["cmd"]
    assert result["pid"] > 0
    pid_file = Path(result["pid_file"])
    assert pid_file.exists()
    assert int(pid_file.read_text().strip()) == result["pid"]

    # Second spawn while the first is alive should refuse (idempotency).
    with pytest.raises(SystemExit, match="already in flight"):
        mod._spawn_detached("line_snapshots", ["--table", "line_snapshots"])

    # Wait for the stub to finish and the marker to land.
    deadline = time.time() + 5
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists()

    # Reap the child so it does not become a zombie for the test runner.
    try:
        os.waitpid(result["pid"], 0)
    except ChildProcessError:
        pass


def test_detach_table_all_spawns_single_orchestrator(tmp_path, monkeypatch):
    """`--detach --table all` must spawn exactly ONE detached child that
    walks every phase sequentially, NOT one child per phase.

    Per-phase forking would race four full imports in parallel (including
    concurrent TRUNCATE + reload of the non-chunked tables), which is
    unsafe and breaks the `PHASE_ORDER` dependency walk.
    """
    monkeypatch.setattr(mod, "WORK_DIR", tmp_path)
    spawn_calls: list[tuple[str, list[str]]] = []

    def fake_spawn(table, argv):
        spawn_calls.append((table, list(argv)))
        return {"pid": 12345, "log": "x", "pid_file": "y"}

    monkeypatch.setattr(mod, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(
        sys, "argv",
        ["import_v1_history", "--table", "all", "--detach", "--chunk-rows", "500000"],
    )

    import asyncio
    rc = asyncio.run(mod._amain())
    assert rc == 0
    assert len(spawn_calls) == 1, (
        f"expected a single orchestrator spawn for --table all, "
        f"got {len(spawn_calls)}: {spawn_calls}"
    )
    table, argv = spawn_calls[0]
    assert table == "all"
    assert "--detach" not in argv
    # The orchestrator child runs the in-process --table all loop, so
    # --table all must be preserved in its argv.
    assert argv[argv.index("--table") + 1] == "all"


def test_detach_single_table_passes_correct_target(tmp_path, monkeypatch):
    """`--detach --table line_snapshots` spawns one child with that
    table preserved in argv and --detach stripped."""
    monkeypatch.setattr(mod, "WORK_DIR", tmp_path)
    spawn_calls: list[tuple[str, list[str]]] = []

    def fake_spawn(table, argv):
        spawn_calls.append((table, list(argv)))
        return {"pid": 12345, "log": "x", "pid_file": "y"}

    monkeypatch.setattr(mod, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(
        sys, "argv",
        ["import_v1_history", "--table", "line_snapshots", "--detach"],
    )

    import asyncio
    rc = asyncio.run(mod._amain())
    assert rc == 0
    assert spawn_calls == [("line_snapshots",
                            ["--table", "line_snapshots"])]
