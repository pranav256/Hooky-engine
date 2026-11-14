"""Exercise actual process trees without media downloads or external API calls."""
import asyncio
import ctypes
import json
import os
import sys
import time
from pathlib import Path
from fastapi.testclient import TestClient

import pytest

from hooky.web import jobs


@pytest.fixture
def fixture_worker(tmp_path, monkeypatch):
    script = tmp_path / "fixture_worker.py"
    script.write_text('''import json, os, subprocess, sys, time
from pathlib import Path
import fastapi
from hooky.config import SUB_STYLE
from hooky.core.lowspec import WHISPER_COMPUTE
Path(__file__).with_name("app-started").write_text("started")
request = json.load(sys.stdin)
mode = request["payload"].get("mode", "sleep")
pid_file = Path(__file__).with_name("grandchild-" + str(os.getpid()))
child_code = "import subprocess,sys,time,fastapi; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time,fastapi;time.sleep(60)']);Path(sys.argv[1]).write_text(str(child.pid));time.sleep(60)"
child = subprocess.Popen([sys.executable, "-c", child_code, str(pid_file)]) if mode in {"sleep", "orphan", "orphan-failure"} else None
if child:
    for _ in range(300):
        if pid_file.exists() and pid_file.read_text().strip(): break
        time.sleep(0.01)
result = {"parent_pid": os.getpid(), "child_pid": child.pid if child else None,
          "grandchild_pid": int(pid_file.read_text()) if pid_file.exists() else None, "setting": SUB_STYLE, "compute": WHISPER_COMPUTE}
print("@@HOOKY_EVENT " + json.dumps({"status": "running", "progress": 35, "result": result}), flush=True)
print("secret " + os.environ.get("GEMINI_API_KEY", ""), flush=True)
if mode == "sleep":
    for index in range(1200):
        print("working", flush=True)
        time.sleep(0.05)
if mode == "complete-on-signal":
    while not Path(__file__).with_name("complete").exists(): time.sleep(0.01)
if mode in {"failure", "orphan-failure"}: sys.exit(9)
print("@@HOOKY_EVENT " + json.dumps({"status": "completed", "progress": 100, "result": result}), flush=True)
''', encoding="utf-8")
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        if "hooky.web.worker" in args or any(isinstance(arg, str) and "runpy.run_module('hooky.web.worker'" in arg for arg in args):
            if os.name == "nt":
                gate = "import sys\nif sys.stdin.buffer.readline()!=b'\\n':raise SystemExit(1)\nimport site,json,runpy,os\nfor path in json.loads(sys.argv[1]):site.addsitedir(path)\nos.environ['PYTHONPATH']=os.pathsep.join(path for path in sys.path if path)\nrunpy.run_path(sys.argv[2],run_name='__main__')"
                args = (sys._base_executable, "-S", "-u", "-c", gate, args[-1], str(script))
            else:
                args = (sys.executable, "-u", str(script))
        return await original(*args, **kwargs)

    monkeypatch.setattr(jobs.asyncio, "create_subprocess_exec", spawn)
    return script


def running(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return not stat.exists() or stat.read_text().split()[2] != "Z"
    except ProcessLookupError:
        return False


async def wait_result(job):
    # Cold imports and descendant startup vary with machine load; this is not a speed check.
    deadline = time.monotonic() + 15
    while not job.get("result"):
        assert time.monotonic() < deadline, job
        await asyncio.sleep(0.01)
    return job["result"]


@pytest.mark.parametrize("shutdown", [False, True])
def test_cancel_and_shutdown_reap_real_worker_and_descendant(tmp_path, fixture_worker, shutdown):
    async def check():
        manager = jobs.JobManager(tmp_path / "history")
        job = await manager.start("analyze", {}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        process = manager.processes[job["id"]]
        result = await wait_result(job)
        assert running(result["parent_pid"]) and running(result["child_pid"])
        if shutdown:
            await manager.shutdown()
        else:
            await manager.cancel(job["id"])
        assert process.returncode is not None
        assert not running(result["parent_pid"]) and not running(result["child_pid"])
        assert not running(result["grandchild_pid"])
        assert job["status"] == ("interrupted" if shutdown else "cancelled")
        assert not manager.tasks and not manager.processes and not manager.secrets and not manager.stopping
        reloaded = jobs.JobManager(manager.directory)
        assert reloaded.jobs[job["id"]]["status"] == job["status"]
    asyncio.run(check())


def test_cancel_during_spawn_and_two_worker_limit(tmp_path, fixture_worker, monkeypatch):
    async def check():
        manager = jobs.JobManager(tmp_path / "history")
        original = jobs.asyncio.create_subprocess_exec
        gate = asyncio.Event()
        entered = asyncio.Event()
        async def delayed(*args, **kwargs):
            if "hooky.web.worker" in args or any(isinstance(arg, str) and "runpy.run_module('hooky.web.worker'" in arg for arg in args):
                entered.set()
                await gate.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(jobs.asyncio, "create_subprocess_exec", delayed)
        first = asyncio.create_task(manager.start("analyze", {}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path))
        await entered.wait()
        first_id = next(iter(manager.jobs))
        cancel = asyncio.create_task(manager.cancel(first_id))
        await asyncio.sleep(0)
        assert manager.jobs[first_id]["status"] == "cancelled"
        gate.set()
        await first
        await cancel
        assert not manager.processes
        if os.name == "nt":
            assert not (tmp_path / "app-started").exists()
        first_job = await manager.start("analyze", {}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        second_job = await manager.start("analyze", {}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        try:
            with pytest.raises(ValueError, match="Two jobs"):
                await manager.start("analyze", {}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        finally:
            await manager.shutdown()
        assert first_job["status"] == second_job["status"] == "interrupted"
    asyncio.run(check())


def test_settings_snapshots_logs_and_terminal_cancel(tmp_path, fixture_worker, monkeypatch):
    from hooky.web import server
    monkeypatch.setattr(server, "ENV_FILE", tmp_path / ".env")
    client = TestClient(server.app, headers={"X-Hooky-Token": server.get_web_token()})
    baseline = server.os.environ.copy()
    async def check():
        manager = jobs.JobManager(tmp_path / "history")
        assert client.post("/api/env", json={"values": {"SUB_STYLE": "gold", "GEMINI_API_KEY": "private-test-key", "WHISPER_COMPUTE": "INT8"}}).status_code == 200
        first = await manager.start("analyze", {}, {**server._runtime_settings(), "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        assert client.post("/api/env", json={"values": {"SUB_STYLE": "clean"}}).status_code == 200
        second = await manager.start("analyze", {"mode": "completed"}, {**server._runtime_settings(), "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        await wait_result(first)
        task = manager.tasks.get(second["id"])
        if task:
            await task
        assert first["result"]["setting"] == "gold"
        assert first["result"]["compute"] == "int8"
        assert second["result"]["setting"] == "clean"
        assert (await manager.cancel(second["id"]))["status"] == "completed"
        await manager.shutdown()
        assert "private-test-key" not in json.dumps(manager.jobs)
        assert "private-test-key" not in "".join(path.read_text(encoding="utf-8") for path in manager.directory.glob("*"))
        assert not manager.secrets
        assert server.os.environ == baseline
    asyncio.run(check())


def test_worker_crash_and_monitor_write_failure_stop_tree(tmp_path, fixture_worker, monkeypatch):
    async def check():
        manager = jobs.JobManager(tmp_path / "history")
        failure = await manager.start("analyze", {"mode": "failure"}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        await manager.tasks[failure["id"]]
        assert failure["status"] == "failed" and failure["exit_code"] == 9
        job = await manager.start("analyze", {}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        process = manager.processes[job["id"]]
        result = await wait_result(job)
        def fail_save(job):
            raise OSError("Disk unavailable")
        monkeypatch.setattr(manager, "_save", fail_save)
        task = manager.tasks[job["id"]]
        await task
        assert job["status"] == "failed" and process.returncode is not None
        assert not running(result["child_pid"])
        assert not running(result["grandchild_pid"])
        assert not manager.tasks and not manager.processes
    asyncio.run(check())


def test_transient_history_write_failure_cannot_prevent_cancellation(tmp_path, fixture_worker, monkeypatch):
    async def check():
        manager = jobs.JobManager(tmp_path / "history")
        job = await manager.start("analyze", {}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        result = await wait_result(job)
        original = manager._save
        failed = False
        def fail_once(job):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("Temporary disk failure")
            original(job)
        monkeypatch.setattr(manager, "_save", fail_once)
        await manager.cancel(job["id"])
        assert failed and job["status"] == "cancelled"
        assert not running(result["parent_pid"]) and not running(result["child_pid"]) and not running(result["grandchild_pid"])
        assert not manager.processes
    asyncio.run(check())


def test_history_retention_interruption_and_log_offsets(tmp_path):
    directory = tmp_path / "history"
    old_cache = tmp_path / "output" / "web_cache" / "analyze_0"
    old_cache.mkdir(parents=True)
    (old_cache / "caption.vtt").write_text("old cache")
    for index in range(105):
        jobs.atomic_json(directory / f"analyze_{index}.json", {"id": f"analyze_{index}", "status": "completed", "created_at": index, "logs": [], "output_root": str(tmp_path / "output")})
    jobs.atomic_json(directory / "analyze_active.json", {"id": "analyze_active", "status": "running", "created_at": 200, "logs": []})
    manager = jobs.JobManager(directory)
    assert len(manager.jobs) == 100
    assert manager.jobs["analyze_active"]["status"] == "interrupted"
    assert len(list(directory.glob("*.json"))) == 100
    assert not old_cache.exists()
    job = manager.jobs["analyze_active"]
    for index in range(205):
        jobs.append_log(job, str(index) + "x" * 3000)
    assert len(job["logs"]) == 200 and job["log_offset"] == 5
    assert all(len(line) <= 2000 for line in job["logs"])
    job["status"] = "running"
    manager.stopping.add(job["id"])
    manager._event_or_log(job, b'@@HOOKY_EVENT {"status":"completed","progress":100}')
    assert job["status"] == "running"
    manager.stopping.clear()
    manager._event_or_log(job, b'@@HOOKY_EVENT {"status":"completed","progress":100,"result":{"bad":NaN}}')
    assert job["status"] == "running"
    manager._event_or_log(job, b'@@HOOKY_EVENT {"status":"running","progress":35,"output_file":null,"result":null}')
    assert job["progress"] == 35
    job.update(type="render_clip", output_root=str(tmp_path / "output"))
    manager._event_or_log(job, b'@@HOOKY_EVENT {"status":"completed","progress":100,"output_file":"missing.mp4"}')
    assert job["status"] == "failed"


def test_cache_cleanup_keeps_exports_and_refuses_redirected_root(tmp_path):
    cache = tmp_path / "output" / "web_cache"
    cache.mkdir(parents=True)
    old = cache / "old.mp4"
    old.write_bytes(b"old")
    os.utime(old, (0, 0))
    new = cache / "new.mp4"
    new.write_bytes(b"new")
    outside = tmp_path / "output" / "reel.mp4"
    outside.write_bytes(b"export")
    jobs.prune_cache(cache, max_bytes=3)
    assert not old.exists() and new.exists() and outside.read_bytes() == b"export"
    redirected = tmp_path / "redirected"
    try:
        redirected.symlink_to(cache, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(redirected), str(cache)], capture_output=True, check=True)
        else:
            raise
    jobs.prune_cache(redirected, max_bytes=0)
    assert new.exists()


@pytest.mark.parametrize("mode", ["orphan", "orphan-failure"])
def test_worker_exit_reaps_stdout_inheriting_descendant_without_waiting_for_eof(tmp_path, fixture_worker, mode):
    async def check():
        manager = jobs.JobManager(tmp_path / "history")
        job = await manager.start("analyze", {"mode": mode}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        task = manager.tasks[job["id"]]
        result = await wait_result(job)
        await asyncio.wait_for(task, 5)
        assert not running(result["child_pid"])
        assert job["status"] == ("completed" if mode == "orphan" else "failed")
        assert not manager.processes and not manager.job_handles
    asyncio.run(check())


def test_completion_does_not_prune_cache_while_another_worker_is_spawning(tmp_path, fixture_worker, monkeypatch):
    async def check():
        manager = jobs.JobManager(tmp_path / "history")
        environment = {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}
        first = await manager.start("analyze", {"mode": "complete-on-signal"}, environment, tmp_path)
        await wait_result(first)
        first_task = manager.tasks[first["id"]]
        original = jobs.asyncio.create_subprocess_exec
        entered, gate = asyncio.Event(), asyncio.Event()
        async def delay(*args, **kwargs):
            entered.set()
            await gate.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(jobs.asyncio, "create_subprocess_exec", delay)
        from unittest.mock import Mock
        prune = Mock()
        monkeypatch.setattr(jobs, "prune_cache", prune)
        second = asyncio.create_task(manager.start("analyze", {"mode": "completed"}, {**environment, "OUTPUT_DIR": str(tmp_path / "output-b")}, tmp_path))
        await entered.wait()
        (tmp_path / "complete").write_text("ready")
        await asyncio.wait_for(first_task, 5)
        prune.assert_not_called()
        gate.set()
        second_job = await second
        await asyncio.wait_for(manager.tasks[second_job["id"]], 5)
        assert {call.args[0] for call in prune.call_args_list} == {tmp_path / "output" / "web_cache", tmp_path / "output-b" / "web_cache"}
    asyncio.run(check())


@pytest.mark.skipif(os.name != "nt", reason="Windows containment bootstrap")
@pytest.mark.parametrize("assignment_fails", [False, True])
def test_windows_worker_stays_gated_until_containment_and_loads_venv_dependencies(tmp_path, fixture_worker, monkeypatch, assignment_fails):
    original = jobs.windows_job
    captured = []
    def assign(pid):
        captured.append(pid)
        assert not (tmp_path / "app-started").exists()
        time.sleep(0.03)
        assert not (tmp_path / "app-started").exists()
        if assignment_fails:
            raise OSError("Assignment refused")
        return original(pid)
    monkeypatch.setattr(jobs, "windows_job", assign)
    async def check():
        manager = jobs.JobManager(tmp_path / "history")
        job = await manager.start("analyze", {"mode": "completed"}, {**os.environ, "OUTPUT_DIR": str(tmp_path / "output")}, tmp_path)
        if assignment_fails:
            assert job["status"] == "failed"
            assert not (tmp_path / "app-started").exists()
        else:
            await asyncio.wait_for(manager.tasks[job["id"]], 5)
            assert job["status"] == "completed"
            assert (tmp_path / "app-started").exists()
        assert not any(running(pid) for pid in captured)
        assert not manager.processes and not manager.job_handles
    asyncio.run(check())
