"""Command line entry point for Hooky's Instagram Reel workflow."""
from __future__ import annotations

import argparse
import gc
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import (AUTO_CLIP_MODE, CHANNELS_FILE, LINKS_FILE, OUTPUT_DIR, POST_HOURS,
                       REELS_MAX_DURATION, SUB_LANG, SUB_STYLE)
from .core import queue as post_queue
from .core.cleanup import delete_clip_artifacts, delete_source_artifacts
from .core.downloader import (check_youtube_access, expand_and_download,
                               parse_links_file)
from .core.queue import load_queue
from .core.run_state import clear_failure, load_failures, record_failure
from .core.source_text import load_caption_file, sidecar_caption_file
from .core.subtitles import STYLES
from .options import PipelineOptions
from .pipeline import Pipeline
from .platforms.instagram import (UploadNotStartedError, is_configured, login,
                                  preflight, upload)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def gather_videos(sources: list[Path]) -> list[Path]:
    """Expand files and folders into a unique list of video files."""
    videos: list[Path] = []
    seen: set[Path] = set()
    output_root = OUTPUT_DIR.resolve()
    for source in sources:
        if source.is_dir():
            found = sorted(
                p for p in source.rglob("*")
                if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
                and output_root not in p.resolve().parents
            )
            if not found:
                print(f"[warn] No videos found in {source}", file=sys.stderr)
            videos.extend(found)
        elif source.exists():
            if source.suffix.lower() not in VIDEO_EXTENSIONS:
                print(f"[warn] Unsupported video type, skipping: {source}",
                      file=sys.stderr)
            else:
                videos.append(source)
        else:
            print(f"[warn] Not found, skipping: {source}", file=sys.stderr)
    unique: list[Path] = []
    for video in videos:
        resolved = video.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(video)
    return unique


def build_parser(prog: str = "hooky") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Hooky finds highlights, renders high-quality vertical clips, and posts them to Instagram.",
        epilog="Pipeline: analyze -> source captions/selected Whisper -> render -> queue -> Instagram.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("sources", nargs="*", type=Path,
                        help="Video file(s) and/or folders")
    parser.add_argument("--clips", default="auto",
                        help="Clips per video: a number or 'auto' (default)")
    parser.add_argument("--auto-mode", choices=("content", "duration"),
                        default=AUTO_CLIP_MODE,
                        help="Auto count by worthwhile moments (default) or source duration")
    parser.add_argument("--min", dest="min_len", type=int, default=15,
                        help="Minimum clip seconds (default: 15)")
    parser.add_argument("--max", dest="max_len", type=int, default=60,
                        help="Maximum clip seconds (default: 60, Reels cap 90)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output directory (default: ./output/VIDEO_NAME/)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process at most N videos")
    parser.add_argument("--no-upload", action="store_true",
                        help="Render clips without adding them to the queue")
    parser.add_argument("--no-post", action="store_true",
                        help="Make runs never post")
    parser.add_argument("--dry-run", action="store_true",
                        help="Select moments without rendering or queueing")
    parser.add_argument("--no-captions", action="store_true",
                        help="Skip Gemini captions")
    parser.add_argument("--no-subs", action="store_true",
                        help="Skip burned-in subtitles and subtitle transcription")
    parser.add_argument("--sub-style", "--style", dest="sub_style", default=SUB_STYLE,
                        help=f"Subtitle style: {', '.join(STYLES)}")
    parser.add_argument("--framing", choices=("auto", "center", "fit"), default="auto",
                        help="Smart vertical framing mode: auto, center (portrait crop), or fit (ambient blur)")
    parser.add_argument("--sub-lang", default=SUB_LANG,
                        help="Force Whisper language, for example 'en'")
    parser.add_argument("--subtitle-source", choices=("auto", "source", "whisper"),
                        default=None,
                        help="Subtitle timing source: source captions when available, or Whisper")
    covers = parser.add_mutually_exclusive_group()
    covers.add_argument("--covers", dest="make_covers", action="store_true",
                        help="Generate Instagram cover images")
    covers.add_argument("--no-covers", dest="make_covers", action="store_false",
                        help="Skip cover images (default from MAKE_COVERS)")
    parser.set_defaults(make_covers=None)
    parser.add_argument("--profile", action="store_true",
                        help="Write timing and cache metrics to the source output directory")
    parser.add_argument("--list-styles", action="store_true",
                        help="List subtitle styles and exit")
    parser.add_argument("--links", nargs="?", const=str(LINKS_FILE), default=None,
                        metavar="FILE", help="Process a links file")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Process only sources recorded as failed in the links file")
    parser.add_argument("--source", action="store_true",
                        help="Scan channels.txt, rank videos by views regardless of upload date, and append unseen winners to links.txt")
    parser.add_argument("--post-due", action="store_true",
                        help="Post queued clips whose cooldown and posting window allow")
    parser.add_argument("--post-now", action="store_true",
                        help="Post the whole queue now with a short gap between clips")
    parser.add_argument("--queue-status", action="store_true",
                        help="Show pending and posted queue entries")
    parser.add_argument("--reset-gap", action="store_true",
                        help="Reset the queue cooldown")
    parser.add_argument("--ignore", action="append", type=Path, metavar="CLIP",
                        help="Remove a clip from the queue (not an uncertain upload)")
    parser.add_argument("--cleanup-posted", action="store_true",
                        help="Remove generated files for clips already recorded as posted")
    review = parser.add_mutually_exclusive_group()
    review.add_argument("--confirm-posted", nargs=2, metavar=("CLIP", "MEDIA_CODE"),
                        help="After checking Instagram, confirm a held upload's media code")
    review.add_argument("--retry-after-review", type=Path, metavar="CLIP",
                        help="After verifying Instagram has no post, explicitly release a held clip for retry")
    parser.add_argument("--check-youtube", metavar="URL", default=None,
                        help="Probe a YouTube URL and verify cookie authentication without downloading")
    parser.add_argument("--check-gemini", action="store_true",
                        help="Verify the Gemini key and model chain")
    return parser


def list_styles() -> None:
    print("Available subtitle styles:")
    for name, style in STYLES.items():
        caps = "ALL CAPS" if style["uppercase"] else "as spoken"
        box = "boxed" if style["border_style"] == 3 else "outlined"
        print(f"  {name:<10} {style['font']:<12} {style['font_size']}px  "
              f"{box}, {caps}, ≤{style['max_words']} words/line")


def _caption_for(clip: Path, entry: dict) -> str:
    caption_file = clip.with_name(clip.stem + ".caption.txt")
    if caption_file.exists():
        try:
            return caption_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return str(entry.get("caption", ""))


def upload_due(state: dict, force: bool = False) -> dict:
    """Post a bounded snapshot with one login and a durable hold per attempt."""
    with post_queue.poster_lock(state):
        return _upload_due_locked(state, force)


def _upload_due_locked(state: dict, force: bool) -> dict:
    stats = {"posted": 0, "failed": 0, "skipped": 0, "remaining": 0, "blocked": 0}
    qpath = post_queue.state_path(state)

    def refresh():
        fresh = load_queue(qpath)
        state.clear()
        state.update(fresh)

    refresh()
    if post_queue.review_holds(state):
        print(f"[queue] {post_queue.status_line(state)}", file=sys.stderr)
        stats.update(blocked=1, remaining=len(state["pending"]))
        return stats
    if not state["pending"]:
        print("[queue] Nothing pending.")
        return stats
    if not force and not post_queue.post_due(state):
        print(f"[queue] Not due yet: {post_queue.status_line(state)}")
        stats["remaining"] = len(state["pending"])
        return stats
    if not is_configured():
        print("[queue] Instagram is not configured: set IG_USERNAME and "
              "IG_PASSWORD in .env", file=sys.stderr)
        stats.update(failed=1, remaining=len(state["pending"]))
        return stats

    client = None
    # New clips enqueued during network requests belong to the next pass. This
    # keeps one explicit --post-now invocation bounded, even with a busy maker.
    for key in list(state["pending"]):
        refresh()
        if post_queue.review_holds(state):
            stats["blocked"] = 1
            break
        if key not in state["pending"]:
            continue
        if not force and not post_queue.post_due(state):
            break
        clip = Path(key)
        entry = state["pending"][key]
        if post_queue.is_posted(clip, state, entry.get("clip_id", "")):
            post_queue.ignore(clip, state)
            continue
        if post_queue.give_up(clip, state):
            print(f"[queue] Retry limit reached for {clip.name}; review before retrying")
            stats["skipped"] += 1
            continue
        if not clip.is_file() or clip.stat().st_size <= 0:
            print(f"[queue] Skipping missing/empty clip: {clip}", file=sys.stderr)
            post_queue.mark_failed(clip, state, "Missing or empty clip")
            stats["skipped"] += 1
            continue

        caption = _caption_for(clip, entry)
        cover = clip.with_name(clip.stem + "_cover.jpg")
        cover = cover if cover.exists() else None
        try:
            if client is None:
                print("[ig] Logging in to Instagram...")
                client = login()
            preflight(clip, client)
        except KeyboardInterrupt:
            raise
        except (Exception, SystemExit) as exc:
            post_queue.mark_failed(clip, state, str(exc))
            stats["failed"] += 1
            print(f"[queue] Before-upload failure: {exc}; stopping this pass", file=sys.stderr)
            break

        gap = post_queue.upload_wait(state)
        if gap > 0:
            print(f"[queue] next upload in {gap / 60:.1f} min")
            time.sleep(gap)
        # This fsynced write MUST succeed before upload() is entered. If this
        # process dies at any later point, the unresolved hold survives it.
        post_queue.begin_upload(clip, state)
        try:
            result = upload(clip, caption=caption, cover=cover, client=client)
        except UploadNotStartedError as exc:
            post_queue.cancel_before_send(clip, state, str(exc))
            stats["failed"] += 1
            print(f"[queue] Before-upload failure: {exc}; stopping this pass", file=sys.stderr)
            break
        except BaseException as exc:
            _record_uncertain(clip, state, exc)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            stats.update(failed=stats["failed"] + 1, blocked=1)
            break
        else:
            try:
                # Invalid/missing confirmations and failures persisting a
                # successful response are just as uncertain as a timeout.
                post_queue.mark_posted(clip, state, result=result)
            except BaseException as exc:
                _record_uncertain(clip, state, exc)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                stats.update(failed=stats["failed"] + 1, blocked=1)
                break
            stats["posted"] += 1
            print(f"[queue] ✓ Instagram: {clip.name} ({result})")
            try:
                removed = delete_clip_artifacts(clip)
                if removed:
                    print(f"[cleanup] Removed posted clip artifacts for {clip.name}")
            except OSError as exc:
                # Upload state is already durable; a cleanup failure must not
                # turn a confirmed post into an unsafe retry.
                print(f"[cleanup] Could not remove posted clip {clip}: {exc}",
                      file=sys.stderr)
            if not force:
                break

    refresh()
    stats["remaining"] = len(state["pending"])
    print(f"[queue] Posting pass done: {stats['posted']} posted, "
          f"{stats['failed']} failed, {stats['remaining']} waiting")
    return stats


def _record_uncertain(clip: Path, state: dict, exc: BaseException) -> None:
    try:
        post_queue.mark_failed(clip, state, str(exc) or type(exc).__name__, retryable=False)
    except Exception as save_exc:
        # The earlier in-flight journal still blocks retries even if this
        # diagnostic update cannot be written (disk full, permission, etc.).
        print(f"[queue] Could not update hold: {save_exc}", file=sys.stderr)
    print(f"[queue] STOPPED: outcome unknown for {clip}. Check Instagram, then use "
          "--confirm-posted CLIP MEDIA_CODE or --retry-after-review CLIP. "
          f"({type(exc).__name__}: {exc})", file=sys.stderr)


def _mark_links_done(links_path: Path, done_lines: list[int],
                     expected: dict[int, str] | None = None) -> bool:
    """Mark only completed, unchanged lines; preserve the rest atomically."""
    marked = True
    with post_queue.file_lock(links_path.with_name(links_path.name + ".lock")):
        lines = links_path.read_text(encoding="utf-8").splitlines()
        for number in done_lines:
            if not 1 <= number <= len(lines):
                marked = False
                continue
            line = lines[number - 1]
            if expected is not None and line.strip() != expected.get(number):
                marked = False
                continue  # someone edited/reordered this source while it ran
            if not line.lstrip().upper().startswith(("# DONE", "DONE ")):
                lines[number - 1] = f"# DONE {line}"
        fd, name = tempfile.mkstemp(prefix=f".{links_path.name}.", suffix=".tmp",
                                    dir=links_path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + ("\n" if lines else ""))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, links_path)
            if os.name != "nt":
                directory = os.open(links_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temp.unlink(missing_ok=True)
    return marked


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _make_source_limit(args) -> int:
    maximum = _positive_env("MAKE_MAX_SOURCES", 2)
    return min(maximum, args.limit) if args.limit > 0 else maximum


def _make_has_capacity(args) -> bool:
    if args.no_upload or args.dry_run:
        return True
    maximum = _positive_env("QUEUE_MAX_PENDING", 12)
    pending = len(load_queue()["pending"])
    if pending >= maximum:
        print(f"[make] Queue has {pending} pending clips (limit {maximum}); "
              "stopping before the next source. Post/review the backlog explicitly.")
        return False
    return True


def ingest_links(args) -> None:
    links_path = Path(args.links or LINKS_FILE)
    if not links_path.exists():
        print(f"[links] {links_path} not found. Run `Hooky setup` to create it.", file=sys.stderr)
        return
    entries = parse_links_file(links_path)
    if args.retry_failed:
        failures = load_failures(links_path)
        entries = [
            entry for entry in entries
            if str(entry["line_no"]) in failures
            and failures[str(entry["line_no"])].get("source") == entry["url"]
        ]
        if not entries:
            print("[links] No retryable failed sources found.")
            return
    if not entries:
        print("[links] No unprocessed lines in the links file.")
        return

    pipeline = Pipeline(PipelineOptions.from_args(args))
    limits = {"clips": 0 if str(args.clips).lower() in ("auto", "0") else int(args.clips),
              "min": args.min_len, "max": args.max_len, "max_sources": 1}
    maximum = _make_source_limit(args)
    processed = 0
    total = len(entries)
    for source_index, entry in enumerate(entries, start=1):
        if processed >= maximum or not _make_has_capacity(args):
            break
        print(f"\n[links] Source {source_index}/{total} — line {entry['line_no']}: "
              f"{entry['url']}")
        print("[links] Phase: reading source metadata")
        # One link is completed (including its queue journal) before the next
        # source is downloaded. Failed source attempts also consume the bound.
        failure_phase = "download"
        failure_error = "No video could be downloaded or found"
        videos, ok = expand_and_download(entry, limits)
        failure_phase = str(entry.get("_failure_phase") or failure_phase)
        failure_error = str(entry.get("_failure_error") or failure_error)
        if not videos:
            if not args.dry_run:
                record_failure(entry["line_no"], entry["url"], phase=failure_phase,
                               error=failure_error, links_path=links_path)
            print(f"[links] Failed source {source_index}/{total}; it remains pending.",
                  file=sys.stderr)
            processed += 1
            continue
        succeeded = True
        for video in videos:
            if processed >= maximum or not _make_has_capacity(args):
                succeeded = False
                break
            processed += 1
            style = video["sub_style"] or args.sub_style
            if style not in STYLES:
                print(f"[links] Unknown style '{style}', using {args.sub_style}",
                      file=sys.stderr)
                style = args.sub_style
            min_len = max(3, int(video["min_len"]))
            max_len = min(REELS_MAX_DURATION, max(5, int(video["max_len"])))
            if min_len >= max_len:
                failure_phase = "validation"
                failure_error = "Invalid min/max clip range"
                print(f"[links] Invalid min/max on line {entry['line_no']}", file=sys.stderr)
                succeeded = False
                continue
            context = ({"title": video["title"], "description": video["description"],
                        "uploader": video["uploader"], "video_id": video.get("video_id", "")}
                       if video["url"] else None)
            try:
                failure_phase = "render"
                failure_error = f"Pipeline failed for {video['path'].name}"
                print(f"[links] Phase: analyzing {video['path'].name}")
                pipeline.options.sub_style = style
                pipeline.options.no_subs = args.no_subs if video["no_subs"] is None else video["no_subs"]
                pipeline.options.clips = (0 if str(video["clips"]).lower() in ("auto", "0")
                                          else video["clips"])
                pipeline.options.min_len = min_len
                pipeline.options.max_len = max_len
                job = pipeline.run(
                    video["path"], context=context, source_url=video["url"] or "",
                    queue_state=None if args.no_upload or args.dry_run else load_queue(),
                    analysis_path=video.get("analysis"),
                    source_transcript=video.get("source_transcript"),
                )
                if not args.dry_run and (not getattr(job, "complete", True)
                                         or (not job.clips and not job.manifest)):
                    succeeded = False
                    failure_error = f"Pipeline did not complete for {video['path'].name}"
                elif not args.dry_run:
                    print(f"[links] Phase: rendered {len(getattr(job, 'clips', []))} clip(s)")
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                succeeded = False
                failure_error = str(exc) or type(exc).__name__
                print(f"[error] {video['path'].name} failed: {exc}", file=sys.stderr)
            finally:
                gc.collect()
        if ok and succeeded and not args.dry_run:
            marked = _mark_links_done(
                links_path, [entry["line_no"]],
                expected={entry["line_no"]: entry["raw_line"]},
            )
            if marked:
                clear_failure(entry["line_no"], links_path)
                print(f"[links] Marked line {entry['line_no']} as DONE")
                for video in videos:
                    removed = delete_source_artifacts(video["path"])
                    if removed:
                        print(f"[cleanup] Removed source artifacts for {video['path'].name}")
            else:
                record_failure(entry["line_no"], entry["url"], phase="source",
                               error="links.txt changed while this source was running",
                               links_path=links_path)
                print(f"[links] Source line {entry['line_no']} changed during the run; "
                      "it remains pending.", file=sys.stderr)
        elif not args.dry_run:
            record_failure(entry["line_no"], entry["url"], phase=failure_phase,
                           error=failure_error, links_path=links_path)
            print(f"[links] Source {source_index}/{total} failed; it remains pending.",
                  file=sys.stderr)
    if processed >= maximum:
        print(f"[make] Source limit reached ({maximum}); remaining links are unchanged.")


def print_queue_status(state: dict) -> None:
    wait = post_queue.wait_seconds(state)
    slot = "ready NOW" if wait <= 0 else f"in {int(wait // 60)}m"
    print(f"Instagram queue: {post_queue.state_path(state)}")
    print(f"  next post {slot} [window {POST_HOURS or 'always'}]")
    for key in post_queue.review_holds(state):
        print(f"  HOLD: {key}")
    if post_queue.review_holds(state):
        print("  Posting blocked. Check Instagram, then --confirm-posted CLIP MEDIA_CODE "
              "or --retry-after-review CLIP (never an automatic retry).")
    print(f"  pending: {len(state['pending'])}")
    for key in list(state["pending"])[:10]:
        print(f"    - {Path(key).name}")
    print(f"  posted:  {len(state['posted'])}")
    for key, posted in list(state["posted"].items())[-5:]:
        print(f"    - {Path(key).name} ({post_queue.format_posted_at(posted)})")
    if state["failed"]:
        print(f"  failed:  {len(state['failed'])}")
    from .core.logbook import tail
    print("\nLogbook (latest):")
    print(tail(10))


def post_now() -> int:
    stats = upload_due(load_queue(), force=True)
    return 2 if stats["failed"] or stats["blocked"] else 0


def main(argv: list[str] | None = None, *, prog: str = "hooky") -> int:
    try:
        return _main(argv, prog=prog)
    except (post_queue.QueueError, ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


def _main(argv: list[str] | None = None, *, prog: str = "hooky") -> int:
    args = build_parser(prog=prog).parse_args(argv)
    if args.list_styles:
        list_styles()
        return 0
    if args.sub_style not in STYLES:
        print(f"[error] Unknown sub_style '{args.sub_style}'", file=sys.stderr)
        return 1
    if args.check_youtube is not None:
        return 0 if check_youtube_access(args.check_youtube) else 2
    if args.check_gemini:
        from .core.gemini import check_gemini
        return check_gemini()
    if args.source:
        from .core.channels import scan_channels
        try:
            import yt_dlp  # noqa: F401 - channel discovery requires this package
        except ImportError:
            print(
                "[source] yt-dlp is required for channel discovery. "
                "Install it with: uv sync --locked --extra all",
                file=sys.stderr,
            )
            return 2
        state = load_queue()
        scan_channels(CHANNELS_FILE, Path(args.links or LINKS_FILE),
                      pending_render_count=len(state.get("pending", {})))
        return 0
    if args.queue_status:
        print_queue_status(load_queue())
        return 0
    if args.confirm_posted:
        clip, code = args.confirm_posted
        post_queue.confirm_posted(Path(clip), code, load_queue())
        print(f"[queue] Confirmed posted: {clip} ({code})")
        try:
            removed = delete_clip_artifacts(Path(clip))
            if removed:
                print(f"[cleanup] Removed posted clip artifacts for {Path(clip).name}")
        except OSError as exc:
            print(f"[cleanup] Could not remove posted clip {clip}: {exc}",
                  file=sys.stderr)
        return 0
    if args.retry_after_review:
        post_queue.retry_after_review(args.retry_after_review, load_queue())
        print("[queue] Review recorded; clip released. Use --post-now or --post-due explicitly.")
        return 0
    if args.reset_gap:
        post_queue.force_post_now(load_queue())
        print("[queue] Schedule cooldown reset (holds and upload safety gap unchanged).")
        return 0
    if args.ignore:
        state = load_queue()
        for clip in args.ignore:
            post_queue.ignore(clip, state)
            print(f"[queue] Removed: {clip}")
        return 0
    if args.cleanup_posted:
        state = load_queue()
        cleaned = 0
        for key in state["posted"]:
            if delete_clip_artifacts(Path(key)):
                cleaned += 1
        print(f"[cleanup] Removed artifacts for {cleaned} posted clip(s)")
        return 0
    if args.post_now:
        return post_now()
    if args.post_due:
        stats = upload_due(load_queue())
        return 2 if stats["failed"] or stats["blocked"] else 0

    if args.limit < 0:
        raise ValueError("--limit must be zero (default bound) or a positive integer")
    if args.min_len < 3 or args.min_len >= args.max_len:
        print("[error] Invalid --min/--max range", file=sys.stderr)
        return 1
    if args.max_len > REELS_MAX_DURATION:
        print(f"[warn] --max exceeds {REELS_MAX_DURATION}s; clamping")
        args.max_len = REELS_MAX_DURATION

    if args.links is not None:
        ingest_links(args)
    else:
        videos = gather_videos(args.sources)
        if not videos:
            print("[error] Pass a video file/folder or use --links", file=sys.stderr)
            return 1
        videos = videos[:_make_source_limit(args)]
        try:
            args.clips = (0 if str(args.clips).lower() in ("auto", "0")
                          else int(args.clips))
        except ValueError:
            print("[error] --clips must be a number or 'auto'", file=sys.stderr)
            return 1
        pipeline = Pipeline(PipelineOptions.from_args(args))
        total_videos = len(videos)
        for source_index, video in enumerate(videos, start=1):
            if not _make_has_capacity(args):
                break
            print(f"\n[run] Source {source_index}/{total_videos}: {video}")
            print("[run] Phase: analyzing and selecting highlights")
            job = pipeline.run(
                video,
                queue_state=None if args.no_upload or args.dry_run else load_queue(),
                source_transcript=load_caption_file(sidecar_caption_file(video)),
            )
            if not args.dry_run and getattr(job, "complete", True):
                print(f"[run] Phase: rendered {len(getattr(job, 'clips', []))} clip(s)")
                removed = delete_source_artifacts(video)
                if removed:
                    print(f"[cleanup] Removed source artifacts for {video.name}")
            gc.collect()

    # Make is deliberately queue-only. Posting always requires a separate,
                    # explicit --post-now or --post-due action (--no-post is a no-op).
    return 0


if __name__ == "__main__":
    sys.exit(main())

