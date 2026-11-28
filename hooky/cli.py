"""Public Hooky command and workflow dispatch."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from . import pipeline_cli
from .config import (CHANNELS_FILE, GEMINI_API_KEY, LINKS_FILE, OUTPUT_DIR,
                     PROJECT_ROOT)

_GIB = 1024 ** 3


def _pipeline(args: Sequence[str], *, prog: str = "hooky") -> int:
    return pipeline_cli.main(list(args), prog=prog)


def _estimate_disk_need(entries: Sequence[dict] = (), *, needs_download: bool = False) -> int:
    """Estimate temporary space for one links run without downloading anything."""
    estimate = 512 * 1024 ** 2  # analysis, subtitles, and transient ffmpeg files
    counted_download = False
    for entry in entries:
        target = str(entry.get("url", ""))
        if target.lower().startswith(("http://", "https://")):
            estimate += 4 * _GIB
            counted_download = True
            continue
        path = Path(target).expanduser()
        try:
            estimate += max(256 * 1024 ** 2, path.stat().st_size * 2)
        except OSError:
            estimate += 1 * _GIB
    if needs_download and not counted_download:
        estimate += 2 * _GIB
    return estimate


def _preflight(*, needs_download: bool = False,
               entries: Sequence[dict] = ()) -> int:
    """Fail early with one actionable message before expensive work starts."""
    failures: list[str] = []
    warnings: list[str] = []
    launcher = ".\\main.ps1" if sys.platform == "win32" else "./main.sh"
    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            failures.append(f"{executable} is missing; run {launcher}")
    if importlib.util.find_spec("google.genai") is None:
        failures.append(f"google-genai is missing; run {launcher}")
    if not os.environ.get("GEMINI_API_KEY", GEMINI_API_KEY).strip():
        failures.append("GEMINI_API_KEY is missing; set it in .env")
    if needs_download and importlib.util.find_spec("yt_dlp") is None:
        failures.append(f"yt-dlp is missing for URL downloads; run {launcher}")
    output_parent = OUTPUT_DIR if OUTPUT_DIR.exists() else OUTPUT_DIR.parent
    if not output_parent.exists() or not os.access(output_parent, os.W_OK):
        failures.append(f"output directory is not writable: {OUTPUT_DIR}")
    state_dir = Path(os.environ.get("STATE_DIR") or PROJECT_ROOT / ".hooky-state")
    state_parent = state_dir if state_dir.exists() else state_dir.parent
    if not state_parent.exists() or not os.access(state_parent, os.W_OK):
        failures.append(f"state directory is not writable: {state_dir}")
    try:
        usage = shutil.disk_usage(output_parent)
        required = _estimate_disk_need(entries, needs_download=needs_download)
        if usage.free < required:
            failures.append(
                f"only {usage.free / _GIB:.1f} GiB free; approximately "
                f"{required / _GIB:.1f} GiB is recommended for this run"
            )
        elif usage.free < required * 2:
            warnings.append(
                f"disk space is low: {usage.free / _GIB:.1f} GiB free, "
                f"about {required / _GIB:.1f} GiB estimated for this run"
            )
    except OSError as exc:
        warnings.append(f"could not inspect free disk space: {exc}")
    if failures:
        print("[hooky] Preflight failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        from .desktop import notify

        notify("Hooky preflight blocked", "; ".join(failures), fallback=False)
        return 2
    for warning in warnings:
        print(f"[hooky] Preflight warning: {warning}", file=sys.stderr)
    return 0


def _completed_link_count() -> int:
    try:
        return sum(
            1 for raw in LINKS_FILE.read_text(encoding="utf-8").splitlines()
            if raw.strip().upper().startswith("DONE ") or "# DONE" in raw.upper()
        )
    except OSError:
        return 0


def _output_clips() -> list[Path]:
    try:
        return [path for path in OUTPUT_DIR.rglob("*.mp4") if path.is_file()]
    except OSError:
        return []


def _link_file_stats() -> tuple[int, int, int]:
    """Return pending links, completed lines, and generated clip count."""
    from .core.downloader import parse_links_file

    return (len(parse_links_file(LINKS_FILE)), _completed_link_count(),
            len(_output_clips()))


def _process_links_file() -> int:
    """Process links.txt and print a compact completion summary."""
    from .core.downloader import parse_links_file
    from .core.run_state import load_failures

    _, done_before, clips_before = _link_file_stats()
    failures_before = load_failures(LINKS_FILE)
    entries = parse_links_file(LINKS_FILE)
    needs_download = any(
        entry.get("url", "").lower().startswith(("http://", "https://"))
        for entry in entries
    )
    preflight = _preflight(needs_download=needs_download, entries=entries)
    if preflight:
        return preflight
    result = _pipeline(["--links", "--no-post"])
    pending_after, done_after, clips_after = _link_file_stats()
    failures_after = load_failures(LINKS_FILE)
    new_failures = any(
        failures_before.get(key) != value for key, value in failures_after.items()
    )
    final_result = result or (2 if new_failures else 0)
    print("[hooky] Run complete" if final_result == 0 else "[hooky] Run finished with errors")
    print(f"[hooky] Links completed this run: {max(0, done_after - done_before)}")
    print(f"[hooky] Clips created this run: {max(0, clips_after - clips_before)}")
    print(f"[hooky] Pending links: {pending_after}")
    print(f"[hooky] Output: {OUTPUT_DIR}")
    print("[hooky] Instagram posts: 0 (posting is a separate command)")
    from .desktop import notify

    notify(
        "Hooky run complete" if final_result == 0 else "Hooky run finished with errors",
        f"{max(0, clips_after - clips_before)} clip(s) created; "
        f"{pending_after} link(s) pending",
    )
    return final_result


def _make(args: Sequence[str]) -> int:
    """Run the simple source-to-queue flow behind ``Hooky make``.

    With no arguments, discovery runs first when channels are configured and
    the resulting links are processed in the same command. Direct video paths
    retain the normal flag-based pipeline.
    """
    if args:
        preflight = _preflight(entries=[])
        if preflight:
            return preflight
        result = _pipeline(["--no-post", *args])
        from .desktop import notify

        notify("Hooky render complete" if result == 0 else "Hooky render failed",
               "Direct source workflow finished")
        return result

    from .core.channels import parse_channels_file
    from .core.downloader import parse_links_file

    channel_entries = parse_channels_file(CHANNELS_FILE)
    link_entries = parse_links_file(LINKS_FILE)
    print("[hooky] Make run: channels.txt -> links.txt -> output/ (no posting).")
    if not channel_entries and not link_entries:
        print("[hooky] Nothing to make yet.")
        print("[hooky] 1. Run `Hooky setup` if you have not done so.")
        print("[hooky] 2. Add a channel to channels.txt or a video/path to links.txt.")
        print("[hooky] 3. Run `Hooky make` again.")
        return 1

    if channel_entries:
        print(f"[hooky] Scanning {len(channel_entries)} configured channel(s)...")
        preflight = _preflight(needs_download=True, entries=link_entries)
        if preflight:
            return preflight
        result = _pipeline(["--source"])
        if result:
            return result

    # Discovery appends to links.txt, so always parse it again before making.
    pending_links = parse_links_file(LINKS_FILE)
    if pending_links:
        print(f"[hooky] Processing {len(pending_links)} pending link(s)...")
        return _process_links_file()

    print("[hooky] No pending links found in links.txt.")
    return 0


def _run_links_file() -> int:
    """Process links.txt by default without scanning channels or posting."""
    from .core.downloader import parse_links_file

    print("[hooky] Default run: links.txt -> output/ (no channel scan, no posting).")
    pending_links = parse_links_file(LINKS_FILE)
    if pending_links:
        print(f"[hooky] Processing {len(pending_links)} pending link(s)...")
        return _process_links_file()

    if not LINKS_FILE.exists():
        print("[hooky] links.txt does not exist yet. Run `Hooky setup` to create it.")
        return 1
    print("[hooky] No pending links found in links.txt.")
    print("[hooky] Add a video URL or local path, then run `Hooky` again.")
    if CHANNELS_FILE.exists():
        from .core.channels import parse_channels_file

        if parse_channels_file(CHANNELS_FILE):
            print("[hooky] To discover channel videos first, run `Hooky make`.")
    return 0


def _post(args: Sequence[str]) -> int:
    if not args:
        command, message = "--post-now", "Instagram posting workflow finished"
    elif list(args) in (["--due"], ["--post-due"]):
        command, message = "--post-due", "Scheduled Instagram posting workflow finished"
    else:
        print("Usage: hooky post [--due]", file=sys.stderr)
        return 1
    result = _pipeline([command])
    from .desktop import notify

    notify("Hooky posting complete" if result == 0 else "Hooky posting failed", message)
    return result


def _open(args: Sequence[str]) -> int:
    if len(args) > 1 or (args and args[0].startswith("-")):
        print("Usage: hooky open [PATH]", file=sys.stderr)
        return 1
    target = Path(args[0]).expanduser() if args else OUTPUT_DIR
    if not target.exists():
        print(f"[hooky] Cannot open missing path: {target}", file=sys.stderr)
        return 1

    from .desktop import open_path

    resolved = target.resolve()
    if open_path(resolved):
        print(f"[hooky] Opened: {resolved}")
        return 0
    print(
        f"[hooky] No desktop opener is available. Open manually: {resolved}",
        file=sys.stderr,
    )
    return 2


def _done_link_target(raw: str) -> str:
    line = raw.strip()
    upper = line.upper()
    if upper.startswith("# DONE"):
        return line[6:].strip().split("|", 1)[0].strip()
    if upper.startswith("DONE "):
        return line[5:].strip().split("|", 1)[0].strip()
    marker = upper.find("# DONE")
    if marker >= 0:
        return line[:marker].strip().split("|", 1)[0].strip()
    return ""


def _completed_source_paths() -> list[Path]:
    """Find source files tied to completed links without adding source state."""
    if not LINKS_FILE.exists():
        return []
    try:
        lines = LINKS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot read {LINKS_FILE}: {exc}") from exc
    completed = {target for target in (_done_link_target(raw) for raw in lines) if target}
    if not completed:
        return []

    paths: list[Path] = []
    urls = {target for target in completed
            if target.lower().startswith(("http://", "https://"))}
    for target in completed - urls:
        path = Path(target).expanduser()
        if path.exists():
            paths.append(path)

    from .core import downloader

    downloads_dir = downloader.DOWNLOADS_DIR
    if downloads_dir.exists():
        for metadata_path in sorted(downloads_dir.glob("*.json")):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(metadata, dict) or metadata.get("url") not in urls:
                continue
            value = metadata.get("file", "")
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute():
                path = metadata_path.parent / path
            if path.exists():
                paths.append(path)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _cleanup(args: Sequence[str]) -> int:
    apply = False
    posted = False
    sources = False
    for value in args:
        if value in {"--apply", "--yes"}:
            apply = True
        elif value == "--posted":
            posted = True
        elif value == "--sources":
            sources = True
        elif value == "--all":
            posted = sources = True
        elif value in {"--help", "help"}:
            print("Usage: hooky cleanup|clean [--apply|--yes] [--posted] [--sources]")
            print("  preview is the default; --apply is required to remove files")
            print("  --posted   clean generated files for confirmed posted clips")
            print("  --sources  clean sources belonging to completed links.txt entries")
            return 0
        else:
            print("Usage: hooky cleanup|clean [--apply|--yes] [--posted] [--sources]",
                  file=sys.stderr)
            return 1

    if not posted and not sources:
        posted = True

    from .core import cleanup as cleanup_helpers

    targets: list[tuple[str, Path, list[Path]]] = []
    seen_targets: set[Path] = set()
    if posted:
        from .core.queue import load_queue

        try:
            state = load_queue()
        except Exception as exc:
            print(f"[cleanup] Cannot read queue history: {exc}", file=sys.stderr)
            return 2
        for key in state.get("posted", {}):
            target = Path(key)
            resolved = target.resolve()
            if resolved in seen_targets:
                continue
            candidates = cleanup_helpers.clip_artifact_candidates(target)
            if candidates:
                seen_targets.add(resolved)
                targets.append(("posted", target, candidates))

    if sources:
        try:
            source_paths = _completed_source_paths()
        except RuntimeError as exc:
            print(f"[cleanup] {exc}", file=sys.stderr)
            return 2
        for target in source_paths:
            resolved = target.resolve()
            if resolved in seen_targets:
                continue
            candidates = cleanup_helpers.source_artifact_candidates(target)
            if candidates:
                seen_targets.add(resolved)
                targets.append(("source", target, candidates))

    print("[cleanup] Preview:")
    if not targets:
        print("[cleanup] No removable artifacts found.")
        return 0
    for kind, target, candidates in targets:
        print(f"  {kind}: {target}")
        for path in candidates:
            print(f"    - {path}")
    total_bytes = 0
    for _, _, candidates in targets:
        for path in candidates:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
    print(f"[cleanup] {sum(len(candidates) for _, _, candidates in targets)} file(s), "
          f"{total_bytes / (1024 ** 2):.1f} MiB eligible")

    if not apply:
        print("[cleanup] Preview only; nothing removed. Re-run with --apply to remove these files.")
        return 0

    removed = 0
    failures = 0
    for kind, target, _ in targets:
        helper = (cleanup_helpers.delete_clip_artifacts
                  if kind == "posted" else cleanup_helpers.delete_source_artifacts)
        try:
            removed += len(helper(target, enabled=True))
        except OSError as exc:
            failures += 1
            print(f"[cleanup] Could not remove {target}: {exc}", file=sys.stderr)
    print(f"[cleanup] Removed {removed} artifact(s)")
    from .desktop import notify

    notify("Hooky cleanup complete", f"{removed} artifact(s) removed")
    return 2 if failures else 0


def _parts(args: Sequence[str], *, number_only: bool = False) -> int:
    from .parts import cli as parts_cli

    forwarded = list(args)
    if number_only:
        forwarded.extend(["--mode", "number-only"])
    return parts_cli.main(forwarded, prog="hooky-parts")


def _source_file(kind: str) -> int:
    """Show the configured text-file sources without creating another database."""
    if kind == "link":
        from .core.downloader import parse_links_file

        path = LINKS_FILE
        entries = parse_links_file(path)
        label = "links"
    else:
        from .core.channels import parse_channels_file

        path = CHANNELS_FILE
        entries = parse_channels_file(path)
        label = "channels"

    print(f"[hooky] {label}: {path}")
    if not path.exists():
        print(f"[hooky] File does not exist yet. Run `Hooky setup` to create it.")
        return 0
    if not entries:
        print(f"[hooky] No active {label} configured.")
        return 0

    print(f"[hooky] {len(entries)} active {label}:")
    for entry in entries:
        target = entry.get("url") or entry.get("target", "")
        options = []
        if kind == "link":
            for key, display in (("clips", "clips"), ("min_len", "min"),
                                 ("max_len", "max"), ("sub_style", "style")):
                value = entry.get(key)
                if value is not None:
                    options.append(f"{display}={value}")
            if entry.get("no_subs") is not None:
                options.append(f"subs={'false' if entry['no_subs'] else 'true'}")
        else:
            for key in ("limit", "min_views", "scan"):
                value = entry.get(key)
                if value is not None:
                    options.append(f"{key}={value}")
        suffix = f" | {' '.join(options)}" if options else ""
        print(f"  {target}{suffix}")
    return 0


def _status() -> int:
    """Print one compact, read-only overview of the local Hooky workspace."""
    from .core.channels import parse_channels_file
    from .core.downloader import parse_links_file
    from .core.queue import load_queue, review_holds, status_line
    from .doctor import dependency_checks

    pending = parse_links_file(LINKS_FILE)
    from .core.run_state import load_failures

    failures = load_failures(LINKS_FILE)
    done = _completed_link_count()
    channels = parse_channels_file(CHANNELS_FILE)

    print("Hooky status")
    print("\nSources")
    print(f"  links.txt:    {len(pending)} pending, {done} completed, "
          f"{len(failures)} failed")
    print(f"  channels.txt: {len(channels)} configured")
    if failures:
        print("\nFailed sources")
        for entry in sorted(failures.values(), key=lambda value: value.get("line", 0)):
            print(f"  line {entry.get('line', '?')}: {entry.get('source', '')}")
            print(f"    phase: {entry.get('phase', 'unknown')}; "
                  f"attempts: {entry.get('attempts', 0)}")
            print(f"    error: {entry.get('error', 'unknown failure')}")

    print("\nRuntime")
    checks = {check.name: check for check in dependency_checks()}
    for name in ("Python >= 3.10", "ffmpeg", "ffprobe", "yt_dlp",
                 "GEMINI_API_KEY", "Instagram credentials"):
        check = checks.get(name)
        if check is None:
            continue
        state = "OK" if check.ok else ("WARN" if not check.required else "FAIL")
        print(f"  {state:<4} {name}: {check.detail}")

    try:
        queue = load_queue()
        print("\nQueue")
        print(f"  pending posts: {len(queue['pending'])}")
        print(f"  posted:        {len(queue['posted'])}")
        print(f"  failed:        {len(queue['failed'])}")
        print(f"  schedule:      {status_line(queue)}")
        if review_holds(queue):
            print("  review:        BLOCKED - inspect Instagram before retrying")
    except Exception as exc:
        print(f"\nQueue\n  FAIL queue unavailable: {exc}")

    clips = _output_clips()
    print("\nOutput")
    print(f"  directory:     {OUTPUT_DIR}")
    print(f"  clips:         {len(clips)}")
    if clips:
        latest = max(clips, key=lambda path: path.stat().st_mtime)
        print(f"  latest:        {latest}")
    state_dir = Path(os.environ.get("STATE_DIR") or PROJECT_ROOT / ".hooky-state")
    print(f"  state:         {state_dir}")
    return 0


def _preview() -> int:
    """Show the pending links plan without network, rendering, or state changes."""
    from .core.downloader import parse_links_file

    entries = parse_links_file(LINKS_FILE)
    print("Hooky preview")
    print("\nSources")
    print(f"  pending links: {len(entries)}")
    if not entries:
        print("  Add a URL or local video path to links.txt before running.")
        return 0

    downloads = 0
    cached = 0
    for entry in entries:
        target = str(entry.get("url", ""))
        if target.lower().startswith(("http://", "https://")):
            downloads += 1
        elif Path(target).expanduser().exists():
            cached += 1
        else:
            print(f"  missing local path: {target}")
    estimate = _estimate_disk_need(entries, needs_download=downloads > 0)
    try:
        free = shutil.disk_usage(OUTPUT_DIR if OUTPUT_DIR.exists() else OUTPUT_DIR.parent).free
        free_line = f"{free / _GIB:.1f} GiB free"
    except OSError:
        free_line = "free space unavailable"

    print("\nActions")
    print("  channel scan: no")
    print(f"  downloads required: {downloads}")
    print(f"  local sources: {cached}")
    print("  rendering: yes")
    print("  Instagram posting: no")
    print("  cleanup: configured source cleanup only after success")
    print(f"  estimated temporary space: {estimate / _GIB:.1f} GiB ({free_line})")
    print("\nNo files were downloaded, rendered, marked DONE, or queued.")
    return 0


def _retry(args: Sequence[str]) -> int:
    if args:
        print("Usage: hooky retry", file=sys.stderr)
        return 1
    from .core.run_state import load_failures

    failures = load_failures(LINKS_FILE)
    if not failures:
        print("[hooky] No retryable failed sources found.")
        return 0
    from .core.downloader import parse_links_file

    entries = [entry for entry in parse_links_file(LINKS_FILE)
               if str(entry["line_no"]) in failures]
    needs_download = any(
        entry.get("url", "").lower().startswith(("http://", "https://"))
        for entry in entries
    )
    preflight = _preflight(needs_download=needs_download, entries=entries)
    if preflight:
        return preflight
    print(f"[hooky] Retrying {len(failures)} failed source(s)...")
    result = _pipeline(["--links", "--no-post", "--retry-failed"])
    remaining = load_failures(LINKS_FILE)
    message = (f"{len(failures) - len(remaining)} recovered; "
               f"{len(remaining)} still failed")
    print(f"[hooky] Retry complete: {message}")
    from .desktop import notify

    notify("Hooky retry complete", message)
    return result or (2 if remaining else 0)


def _doctor(args: Sequence[str]) -> int:
    if not args or args[0] in {"dependencies", "deps"}:
        from .doctor import run_doctor

        return run_doctor()
    if args[0] in {"live", "--live", "gemini", "--gemini"}:
        from .core.gemini import check_gemini

        return check_gemini()
    if args[0] in {"--help", "help"}:
        print("Usage: hooky doctor [dependencies|youtube [URL]]")
        print("  dependencies  check local runtimes and optional integrations")
        print("  live          live API key and model rotation check against Gemini")
        print("  youtube URL   validate cookies and probe a YouTube URL")
        return 0
    if args[0] != "youtube":
        print("Usage: hooky doctor [dependencies|youtube [URL]]", file=sys.stderr)
        return 1
    from .core.downloader import check_youtube_access, youtube_auth_status

    status = youtube_auth_status()
    print(f"[youtube] method: {status['method']}")
    print(f"[youtube] {status['message']}")
    if not status["ok"]:
        return 2
    if len(args) > 1:
        return 0 if check_youtube_access(args[1]) else 2
    return 0


def _completion(args: Sequence[str]) -> int:
    from .completion import SHELLS, script

    if len(args) == 1 and args[0] in SHELLS:
        print(script(args[0]), end="")
        return 0
    print("Usage: hooky completion bash|zsh|fish|powershell", file=sys.stderr)
    return 1


def _help() -> int:
    pipeline_cli.build_parser(prog="hooky").print_help()
    print("\nHooky commands:")
    print("  hooky (no args)             process pending entries from links.txt")
    print("  hooky links                  show active entries from links.txt")
    print("  hooky channels               show active entries from channels.txt")
    print("  hooky status                 show sources, runtime, queue, and output")
    print("  hooky preview                show pending work without changing files")
    print("  hooky retry                  retry failed source-processing entries")
    print("  hooky open [PATH]            open output/ or a specified file/directory")
    print("  hooky cleanup [--apply]      preview, then remove confirmed artifacts")
    print("  hooky completion SHELL      print native bash/zsh/fish/PowerShell completion")
    print("  hooky doctor                check local dependencies and install hints")
    print("  hooky doctor live           live Gemini key & model rotation check")
    print("  hooky doctor youtube [URL]  validate cookies or probe YouTube")
    print("  hooky setup [--redo|--show] configure Hooky for this machine")
    print("  hooky make|parts|post        run the existing workflows")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run Hooky's normal command-line workflows."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _run_links_file()

    command, rest = args[0], args[1:]
    if command in {"web", "ui", "gui", "portal", "serve"}:
        from .web.server import run_server
        return run_server(rest)
    if command == "help" or args == ["--help"]:
        return _help()
    if command == "setup":
        from .setup import cli as setup_cli

        return setup_cli.main(rest)
    if command == "completion":
        return _completion(rest)
    if command == "__complete":
        from .completion import complete_command

        return complete_command(rest)
    if command == "open":
        return _open(rest)
    if command in {"cleanup", "clean"}:
        return _cleanup(rest)
    if command == "preview":
        if rest:
            print("Usage: hooky preview", file=sys.stderr)
            return 1
        return _preview()
    if command == "retry":
        return _retry(rest)
    if command == "links":
        return _source_file("link")
    if command == "channels":
        return _source_file("channel")
    if command == "status":
        return _status()
    if command == "doctor":
        return _doctor(rest)
    if command in {"add", "catalog", "sources", "import", "run-source"}:
        print(
            "[hooky] Source management uses channels.txt and links.txt. "
            "Edit those files directly.",
            file=sys.stderr,
        )
        return 1
    if command == "make":
        return _make(rest)
    if command == "parts":
        return _parts(rest)
    if command in {"number-parts", "number_only"}:
        return _parts(rest, number_only=True)
    if command == "post":
        return _post(rest)
    if command in {"source", "scan"}:
        return _pipeline(["--source", *rest])
    if command in {"queue", "queue-status"}:
        return _pipeline(["--queue-status", *rest])
    if command == "styles":
        return _pipeline(["--list-styles", *rest])

    # Preserve the existing flag-based pipeline interface and explicit paths.
    return _pipeline(args)


__all__ = ["main"]

