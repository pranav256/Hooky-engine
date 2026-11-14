Hooky engine and Studio performance research, 14 November 2026. Baseline: `7a6fbab`.

The useful first improvement is to avoid preparing the same source for every export. The CLI already has transcript checkpoints, media probe caches, bounded threads and opt-in profiling. Studio used a fresh directory per worker, so its saved `cues.json` was never read by subsequent workers. Its worker also emitted the entire unchanged status every 200 ms; the supervisor saved each incoming chunk with an atomic write and `fsync`.

This change keeps the current Python, FFmpeg and faster-whisper stack. It adds reusable source checkpoints, overlaps independent metadata/caption requests, and emits status only when it changes. It also fixes Studio's Whisper fallback to respect the existing `SUB_LANG` setting, keeps normalized caption tracks separated by source/language prefix, and isolates API test settings from the local `.env` file. No dependencies were added.

The checkpoint uses the existing atomic checkpoint helper and stays under `output/web_cache`, within the existing seven-day/1 GiB cleanup policy. It expires after 24 hours and includes the URL, caption languages, subtitle source, transcription language, Whisper model/compute mode and a digest of authentication arguments. Styling and framing changes can reuse the transcript. Empty, expired, mismatched or malformed entries are recomputed. Cache write failures leave the prepared transcript usable; exports still publish only after a successful encode.

Limits: remote captions or metadata can change during the 24-hour window. Updating cookie contents at the same configured path does not change the cache key. Two simultaneous first-time jobs for the same source may both prepare it; atomic publication prevents a partial shared entry, but there is no cross-process request lock. Adding a lock should wait for evidence that duplicate concurrent preparation is common.

An offline before/after experiment compared the original `prepare_source` function from Git with the updated function. Metadata and captions each had a simulated 100 ms wait, with the same short VTT track. Each result below is the median of five runs on this Windows environment and includes `asyncio.run` startup/shutdown. These are preparation measurements, not real YouTube, Whisper, Gemini or video-encoding speed claims.

| Preparation | Baseline | Updated |
| --- | ---: | ---: |
| New source | 215.80 ms | 107.30 ms |
| Same source in another worker directory | 216.14 ms | 13.13 ms |
| Metadata requests across five new + five repeated jobs | 10 | 5 |
| Caption requests across five new + five repeated jobs | 10 | 5 |

The concurrency regression check uses a synchronization event to prove captions start while metadata is waiting. Cache tests cover reuse across worker directories, settings changes, expired/corrupt data, empty sources, Whisper fallback and cache write errors. A worker test verifies initial, changed and terminal progress events without duplicate unchanged events. The full offline suite passed: 606 tests, 7 opt-in integration skips. Four real FFmpeg Studio checks passed separately: center/fit framing with fractional boundaries and visible captions, invalid-download rejection, and cancellation before publication.

Upstream research supports the following decisions:

| Source reviewed | Relevant evidence | Decision for Hooky |
| --- | --- | --- |
| [faster-whisper README](https://github.com/SYSTRAN/faster-whisper#benchmark) | CPU int8 and GPU execution already exist. Its published CPU small-model batch example uses 3,608 MB versus 1,477 MB without batching. Comparisons must use matching beam sizes and thread counts. These are upstream measurements on different hardware. | Keep the current int8, beam-size-1, bounded-thread default. Evaluate optional GPU transcription separately; do not turn on batching by default for 4 GB machines. |
| [yt-dlp README](https://github.com/yt-dlp/yt-dlp#usage-and-options) | `--download-sections` supports selected ranges. `--force-keyframes-at-cuts` requires re-encoding and reduces cut artifacts. Caption-only downloads and manual/automatic caption tracks are supported. | Preserve exact-cut behavior and selected-section downloads. Removing the first encode needs a separate timestamp/artifact experiment before changing defaults. |
| [FastAPI concurrency guide](https://fastapi.tiangolo.com/async/) | Blocking helpers called directly by an async function do not automatically move to its thread pool. Waiting on independent I/O can overlap. | Keep blocking probes/transcription off the async loop, and overlap independent source requests. |
| [OpusClip product page](https://www.opus.pro/) | The product describes editable captions, clip selection, and automatic relayout. These are product claims, not independently verified accuracy or virality measurements. | Prioritize user control over adding more subtitle styles or cosmetic effects. |
| [WhisperX README](https://github.com/m-bain/whisperX) | Word alignment and speaker diarization are available, but add models and processing; its speaker workflow requires a Hugging Face token and model access. | Treat accurate alignment/speaker detection as optional quality features with explicit hardware and setup costs. |

Recommended features, in build order. These are researched proposals, not implemented features. See the [expanded feature research](feature-research.md) for additional options, primary-source evidence and implementation scope.

| Priority | Feature | Smallest useful version | Value / effort |
| --- | --- | --- | --- |
| 1 | Edit and search the transcript | Search timestamped cues, fix a caption, jump to its time, and reuse those edits during export. Validate timing and preserve the original. | High value; medium effort. Reuses prepared cues and ASS generation. |
| 2 | Export selected hooks together | Select several existing candidates, use the same style/framing, and export within the current two-worker limit. Keep each finished file visible if another fails. | High value; medium effort. Reuses the shared renderer and job history. |
| 3 | Save a source draft | Save selected ranges, corrected captions and framing/style choices in one versioned JSON file. Reopen and edit without paid re-analysis. | High value; medium effort. Reuses checkpoints; no database needed. |
| 4 | Explain candidate rankings honestly | Show the words, speech pacing and source evidence behind each recommendation. Replace rank-derived percentage scores with a clearly labeled heuristic score unless calibrated against real outcomes. | High trust value; small to medium effort. Current local fallback scores are assigned by rank, not measured retention. |
| 5 | Fast draft preview | Render a short, lower-resolution preview before the final 1080x1920 export. Preserve final quality defaults and reuse source preparation. | Useful for slower hardware; medium effort. Requires a deliberate output profile and visual checks. |
| 6 | Speaker-aware cropping | Start with an editable fixed crop per scene; later add optional active-speaker tracking with a fit-mode fallback. | High value for interviews; high effort for automatic tracking. Speaker diarization alone does not locate faces in video. |

For the next speed investigation, use the existing CLI `--profile` with a representative local video and separate source preparation, transcription and rendering. Compare center and fit framing, elapsed time, memory, file size and visible/audio quality at identical dimensions and frame rate. The renderer currently selects CPU `libx264`, and transcription selects CPU faster-whisper. Optional hardware acceleration needs a real encode/transcription probe and CPU fallback; this change does not claim a measured GPU gain.

A Rust rewrite, a new queue/database, more animation styles and a cloud pipeline were skipped. Most heavy work already runs inside native FFmpeg/CTranslate2, so changing the orchestration language alone is not evidence of faster exports. Real YouTube authentication, paid Gemini calls, GPU behavior, and end-to-end production-video timing were not checked. Browser layout was unchanged and was not visually reviewed in this backend-focused change.
