"""Durable Studio jobs, each isolated in its own process tree."""
from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4
from hooky.dotenv import SECRET_KEYS, MASK

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
MAX_JOBS = 100
MAX_LOGS = 200
MAX_ACTIVE = 2
EVENT_PREFIX = "@@HOOKY_EVENT "
MAX_EVENT_BYTES = 1024 * 1024
MAX_LOG_LENGTH = 2000


def windows_job(pid: int):
    """Assign a still-gated native worker before it can start descendants."""
    from ctypes import wintypes
    size = ctypes.c_size_t
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working", size), ("max_working", size),
                    ("active_limit", wintypes.DWORD), ("affinity", size),
                    ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", size), ("job_memory", size), ("peak_process", size), ("peak_job", size)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    process_handle = kernel.OpenProcess(0x0101, False, pid)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not handle or not process_handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, process_handle):
            raise OSError(ctypes.get_last_error(), "Could not contain the Studio worker process.")
        return handle
    except BaseException:
        if handle:
            kernel.CloseHandle(handle)
        raise
    finally:
        if process_handle:
            kernel.CloseHandle(process_handle)


def close_windows_job(handle) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle(handle)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".job-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        Path(name).replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        Path(name).unlink(missing_ok=True)


def append_log(job: dict, line: str) -> None:
    job.setdefault("logs", []).append(line[:MAX_LOG_LENGTH])
    excess = len(job["logs"]) - MAX_LOGS
    if excess > 0:
        del job["logs"][:excess]
        job["log_offset"] = job.get("log_offset", 0) + excess
    job["updated_at"] = time.time()


def prune_cache(path: Path, max_bytes: int = 1024 * 1024 * 1024, max_age: float = 7 * 86400) -> None:
    """Only disposable Studio media is eligible; pipeline manifests stay intact."""
    if not path.exists() or _redirected(path):
        return
    root = path.resolve()
    files = []
    for file in path.rglob("*"):
        if file.is_file() and not _redirected(file) and file.resolve().is_relative_to(root):
            stat = file.stat()
            files.append((stat.st_mtime, stat.st_size, file))
    total = sum(size for _, size, _ in files)
    cutoff = time.time() - max_age
    for modified, size, file in sorted(files):
        if modified < cutoff or total > max_bytes:
            if file.resolve().is_relative_to(root):
                file.unlink(missing_ok=True)
            total -= size
    for directory in sorted((item for item in path.rglob("*") if item.is_dir() and not _redirected(item)), key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def _redirected(path: Path) -> bool:
    return path.is_symlink() or os.path.normcase(os.path.abspath(path)) != os.path.normcase(str(path.resolve()))


def remove_job_cache(job: dict) -> None:
    if not job.get("output_root"):
        return
    root = Path(job["output_root"]) / "web_cache"
    cache = root / job["id"]
    if cache.exists() and not _redirected(cache) and not _redirected(root) and cache.resolve().is_relative_to(root.resolve()):
        try:
            shutil.rmtree(cache)
        except OSError:
            pass


async def stop_process_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        if process.returncode is not None:
            await process.wait()
            return
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            pass
        # Reap descendants even if the worker exited before an ignoring child.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


class JobManager:
    def __init__(self, directory: Path, jobs: dict | None = None):
        self.directory = directory
        self.jobs = jobs if jobs is not None else {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.stopping: set[str] = set()
        self.starting = 0
        self.spawning: dict[str, asyncio.Event] = {}
        self.secrets: dict[str, tuple[str, ...]] = {}
        self.job_handles: dict[str, Any] = {}
        if directory.exists():
            for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(job, dict) or job.get("id") != path.stem or not isinstance(job.get("logs"), list):
                        continue
                    job["logs"] = job["logs"][-MAX_LOGS:]
                    if job.get("status") not in TERMINAL:
                        job.update(status="interrupted", step="Studio restarted; start a new job to retry.", updated_at=time.time())
                        atomic_json(path, job)
                    self.jobs[job["id"]] = job
                except (OSError, ValueError, TypeError):
                    continue
        self._prune()

    def _save(self, job: dict) -> None:
        atomic_json(self.directory / (job["id"] + ".json"), self._redact(job, self.secrets.get(job["id"], ())))

    def _redact(self, value, secrets: tuple[str, ...]):
        if isinstance(value, dict):
            return {key: self._redact(item, secrets) for key, item in value.items() if key not in SECRET_KEYS and key != "output_url"}
        if isinstance(value, list):
            return [self._redact(item, secrets) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, MASK)
        return value

    def _prune(self) -> None:
        terminal = sorted((job for job in self.jobs.values() if job.get("status") in TERMINAL), key=lambda job: job.get("created_at", 0))
        while len(terminal) > MAX_JOBS:
            job = terminal.pop(0)
            remove_job_cache(job)
            self.jobs.pop(job["id"], None)
            self.secrets.pop(job["id"], None)
            self.stopping.discard(job["id"])
            (self.directory / (job["id"] + ".json")).unlink(missing_ok=True)

    async def start(self, kind: str, payload: dict, environment: dict[str, str], project_root: Path) -> dict:
        if len(self.processes) + self.starting >= MAX_ACTIVE:
            raise ValueError("Two jobs are already running. Wait for one to finish or cancel it before starting another.")
        job_id = f"{kind}_{uuid4().hex}"
        now = time.time()
        job = {"id": job_id, "type": "render_clip" if kind == "render" else kind, "status": "running", "created_at": now, "updated_at": now,
               "progress": 5, "step": "Starting isolated worker...", "step_number": 0,
               "logs": [], "log_offset": 0, "result": None, "output_file": None,
               "url": payload.get("url", ""), "target": payload.get("target", ""), "title": payload.get("title", "")}
        job["request"] = payload
        self.secrets[job_id] = tuple(value for key, value in environment.items() if key in SECRET_KEYS and value)
        output = Path(environment.get("OUTPUT_DIR") or "output")
        job["output_root"] = str(output if output.is_absolute() else project_root / output)
        self.jobs[job_id] = job
        self._save(job)
        self._prune()
        code_root = str(Path(__file__).resolve().parents[2])
        env = {**environment, "HOOKY_SETTINGS_SNAPSHOT": "1", "HOOKY_PROJECT_ROOT": str(project_root),
               "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "PYTHONPATH": code_root + os.pathsep + environment.get("PYTHONPATH", "")}
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        self.starting += 1
        spawned = asyncio.Event()
        self.spawning[job_id] = spawned
        try:
            command = [sys.executable, "-u", "-m", "hooky.web.worker"]
            if os.name == "nt":
                bootstrap = "import sys\nif sys.stdin.buffer.readline() != b'\\n': raise SystemExit(1)\nimport site,runpy,json,os\nfor path in json.loads(sys.argv[1]): site.addsitedir(path)\nos.environ['PYTHONPATH']=os.pathsep.join(path for path in sys.path if path)\nsys.argv=['hooky.web.worker']\nrunpy.run_module('hooky.web.worker',run_name='__main__')"
                paths = list(dict.fromkeys((sysconfig.get_path("purelib"), sysconfig.get_path("platlib"))))
                env["PYTHONPATH"] = os.pathsep.join(paths + [env["PYTHONPATH"]])
                command = [sys._base_executable, "-S", "-u", "-c", bootstrap, json.dumps(paths)]
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(project_root), env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options,
            )
            if os.name == "nt":
                try:
                    self.job_handles[job_id] = windows_job(process.pid)
                except BaseException:
                    process.stdin.close()
                    await stop_process_tree(process)
                    raise
        except Exception as exc:
            if job_id not in self.stopping:
                job.update(status="failed", error=str(exc), updated_at=time.time())
            self._save(job)
            sanitized = self._redact(job, self.secrets.pop(job_id, ()))
            job.clear()
            job.update(sanitized)
            self.spawning.pop(job_id, None)
            spawned.set()
            return job
        finally:
            self.starting -= 1
        self.processes[job_id] = process
        self.tasks[job_id] = asyncio.create_task(self._monitor(job, process))
        if job_id in self.stopping:
            # A cancelled native Windows worker never passes its import gate.
            process.stdin.close()
            await self._stop(job_id, process)
            self.spawning.pop(job_id, None)
            spawned.set()
            return job
        self.spawning.pop(job_id, None)
        spawned.set()
        process.stdin.write((b"\n" if os.name == "nt" else b"") + json.dumps({"job": job, "payload": payload}).encode("utf-8"))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        process.stdin.close()
        return job

    async def _monitor(self, job: dict, process) -> None:
        pending = b""
        async def reap_descendants():
            while process.returncode is None:
                await asyncio.sleep(0.02)
            handle = self.job_handles.pop(job["id"], None)
            if handle:
                close_windows_job(handle)
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()
        watcher = asyncio.create_task(reap_descendants())
        try:
            while chunk := await process.stdout.read(8192):
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    self._event_or_log(job, line)
                if len(pending) > MAX_EVENT_BYTES:
                    append_log(job, "Worker emitted an oversized line; discarded.")
                    pending = b""
                self._save(job)
            if pending:
                self._event_or_log(job, pending)
            await process.wait()
            job["exit_code"] = process.returncode
            if job["id"] not in self.stopping:
                if process.returncode != 0 or job.get("status") not in TERMINAL:
                    job.update(status="failed", error=job.get("error") or f"Worker exited with code {process.returncode} before completing.")
            job["updated_at"] = time.time()
            self._save(job)
        except BaseException as exc:
            self.stopping.add(job["id"])
            if job.get("status") not in TERMINAL:
                job.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed", error="Worker supervision failed; check state-directory permissions.", updated_at=time.time())
            await self._stop(job["id"], process)
            try:
                self._save(job)
            except OSError:
                pass
        finally:
            await watcher
            sanitized = self._redact(job, self.secrets.get(job["id"], ()))
            job.clear()
            job.update(sanitized)
            self.secrets.pop(job["id"], None)
            self.processes.pop(job["id"], None)
            self.tasks.pop(job["id"], None)
            self._prune()
            self.stopping.discard(job["id"])
            if job.get("status") in {"failed", "cancelled", "interrupted"}:
                remove_job_cache(job)
            if not self.processes and not self.starting:
                for output in {item["output_root"] for item in self.jobs.values() if item.get("output_root")}:
                    try:
                        prune_cache(Path(output) / "web_cache")
                    except OSError:
                        pass

    def _event_or_log(self, job: dict, line: bytes) -> None:
        decoded = line.decode("utf-8", errors="replace").rstrip()
        if not decoded.startswith(EVENT_PREFIX):
            if decoded:
                append_log(job, self._redact(decoded, self.secrets.get(job["id"], ())))
            return
        if job["id"] in self.stopping or job.get("status") in TERMINAL or len(line) > MAX_EVENT_BYTES:
            return
        try:
            def invalid_constant(value):
                raise ValueError("Non-finite event data")
            event = json.loads(decoded[len(EVENT_PREFIX):], parse_constant=invalid_constant)
            if not isinstance(event, dict):
                return
            if event.get("status") not in {"running", *TERMINAL}:
                return
            if any(key in event and event[key] is not None and (not isinstance(event[key], str) or len(event[key]) > MAX_LOG_LENGTH) for key in ("step", "error", "output_file")):
                return
            if "step_number" in event and (isinstance(event["step_number"], bool) or not isinstance(event["step_number"], int) or not 0 <= event["step_number"] <= 100):
                return
            if "result" in event and event["result"] is not None and not isinstance(event["result"], dict):
                return
            if "output_files" in event and (not isinstance(event["output_files"], list) or len(event["output_files"]) > 100 or any(not isinstance(item, str) for item in event["output_files"])):
                return
            event = self._redact(event, self.secrets.get(job["id"], ()))
            if event["status"] == "completed" and job["type"] in {"render_clip", "make"}:
                root = Path(job["output_root"]).resolve()
                output = event.get("output_file")
                path = root / output if output else None
                if not path or not path.resolve().is_relative_to(root) or path.suffix.lower() != ".mp4" or not path.is_file() or path.stat().st_size <= 1000 or "web_cache" in path.relative_to(root).parts:
                    event.update(status="failed", error="Worker reported completion without a complete reel.", output_file=None)
            progress = event.get("progress", 0)
            if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(progress):
                return
            job["progress"] = max(0, min(100, progress))
            for key in ("status", "step", "step_number", "result", "output_file", "output_files", "error"):
                if key in event:
                    job[key] = event[key]
            job["updated_at"] = time.time()
        except (OSError, ValueError, TypeError, RecursionError):
            pass

    async def cancel(self, job_id: str, status: str = "cancelled") -> dict:
        job = self.jobs[job_id]
        if job.get("status") in TERMINAL:
            spawned = self.spawning.get(job_id)
            if spawned:
                await spawned.wait()
            process = self.processes.get(job_id)
            if process:
                self.stopping.add(job_id)
                await self._stop(job_id, process)
                task = self.tasks.get(job_id)
                if task:
                    await task
                self.stopping.discard(job_id)
            return job
        self.stopping.add(job_id)
        job.update(status=status, step="Job cancelled." if status == "cancelled" else "Studio shut down; start a new job to retry.", updated_at=time.time())
        try:
            self._save(job)
        except OSError:
            # Persistence cannot prevent cancellation of a live process tree.
            pass
        try:
            spawned = self.spawning.get(job_id)
            if spawned:
                await spawned.wait()
            process = self.processes.get(job_id)
            if process:
                await self._stop(job_id, process)
            task = self.tasks.get(job_id)
            if task:
                await task
        finally:
            self.stopping.discard(job_id)
        self._save(job)
        return job

    async def _stop(self, job_id: str, process) -> None:
        handle = self.job_handles.pop(job_id, None)
        if handle:
            close_windows_job(handle)
            await process.wait()
        else:
            await stop_process_tree(process)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.cancel(job_id, "interrupted") for job_id in set(self.processes) | set(self.spawning)))
