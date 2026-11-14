Hooky feature research — 14 November 2026.

The strongest direction is to let creators ask for a particular kind of clip, correct its captions, and export the versions they actually want. Hooky already has the expensive foundations: source transcripts, Gemini selection, timed captions, a shared renderer, job cancellation and checkpoints. The proposals below extend those foundations. Topic search, caption editing and named projects were subsequently implemented; the remaining features remain proposals.

Implementation: optional topic search reads every transcript chunk using the existing Gemini/checkpoint helpers and falls back to explicitly labeled word matches. Results retain their actual source cues. The caption editor supports phrase search, correction, reset and current-browser session restoration; validated edits are applied to a copy of the source cues during export. Named projects now save complete editor snapshots as versioned local JSON, independent of job history, with explicit save/open/copy and protection against stale saves. Batch export, cloud sync and text-driven removal of video segments remain proposals. See the README for use and verification notes in `docs/studio-features-validation.md` and `docs/studio-projects-validation.md`.

**Best additions**

| Feature | What the creator does | Smallest useful version | Effort / running cost |
| --- | --- | --- | --- |
| Clip by topic or instruction | “Find three moments about startup mistakes” or “Find the practical advice.” | Search the entire prepared transcript, collect relevant windows, and use the existing Gemini path to choose bounded, non-overlapping clips. Show the supporting dialogue beside each result. | Medium; keyword search can be local, semantic selection uses Gemini. |
| Correct and search captions | Fix a person's name, find a quote, and export the corrected text. | Editable timestamped phrases with search, undo/reset and preview. Persist edits separately from the reusable source transcript. | Medium; ordinary editing needs no new model. |
| Export packs | Download a reel with editable subtitles and posting text. | MP4 + SRT/VTT + title/caption text in a ZIP; optional cover using the existing cover helper. Subtitle timestamps start at zero for each clip. | Small to medium; subtitle/text sidecars are cheap, a cover decodes a frame. |
| Named projects and selected batch export | Keep several sources, save edits, select three hooks, and export those three. | One versioned project JSON per source/project, with ranges, edited cues, style/framing and selected clips. Reuse one transcript inside a batch job. | Medium; no database or additional AI required for reopening. |
| Manual crop and output formats | Move the crop toward a speaker or slide, then choose portrait, square or landscape. | One editable fixed crop per clip, with preview and fit fallback. Preserve the current portrait export as the default. | Medium; native FFmpeg filters suffice, but previews and output profiles need checking. |
| Pause cleanup | Review long silent gaps and keep/remove selected gaps. | Detect silence in a selected clip, show proposed removals, and apply the same approved cuts to audio, video and captions. Start with silence, before filler words. | Medium to high; local CPU processing, no new speech model for silence detection. |
| Translated subtitle variants | Export the same clip with Hindi or another selected caption language. | Translate phrases while preserving source timing, let the user correct translations, and render separate variants with suitable fonts. | Medium to high; translation needs a service/model and text may need more space. |
| Automatic speaker framing | Keep a moving speaker in the crop. | Begin with fixed manual crops; later add optional visual tracking and speaker-aware shot changes with fit fallback. | High; additional inference and hardware/quality testing. |
| Optional YouTube publishing | Send an approved completed clip to the creator's channel. | Explicit account connection, title/description/privacy selection, resumable upload and stored upload IDs to prevent duplicate retries. | Medium to high; OAuth and a distinct publishing integration. |

Effort is a relative engineering estimate, not a delivery quote. No competitor subscription prices or accuracy claims are used to justify the ranking.

**What the research supports**

| Primary source inspected | Evidence | Implication for Hooky |
| --- | --- | --- |
| [OpusClip ClipAnything](https://www.opus.pro/clipanything) | Describes finding moments through prompts and visual, audio and sentiment cues, including low-dialogue video. | Instruction-based clipping is a useful product direction. Hooky's first version should explicitly search speech; matching a visual event such as “the dog jumps” requires additional video analysis. |
| [Descript video editing](https://www.descript.com/video-editing) | Describes editing audio/video through text and adding captions. | A transcript editor can reduce correction work and give creators direct control over generated clips. It is a stronger foundation than adding more decorative caption styles. |
| [OpusClip AI Reframe](https://www.opus.pro/ai-reframe) | Describes manual reframing, multiple aspect ratios, and tracking moving speakers/objects. | Manual crop adjustment is a practical first release. Full tracking is a separate, larger feature. The vendor's speed and quality claims were not independently tested. |
| [Descript filler-word removal](https://www.descript.com/filler-words) | Offers removal of “ums,” “uhs” and other filler phrases. | Cleanup can save editing time, but should start as reviewable suggestions. Source captions often omit fillers, and approximate word timing is insufficient for clean word-level cuts. |
| [FFmpeg silencedetect/silenceremove](https://ffmpeg.org/ffmpeg-filters.html#silencedetect) | Detects silence using configurable volume/duration thresholds. `silenceremove` operates on audio. | Silence detection can use the installed media backend. Applying audio-only removal would desynchronize video; build one approved cut plan for audio, video and subtitle timing. Background music can hide pauses from amplitude detection. |
| [WhisperX](https://github.com/m-bain/whisperX#limitations) | Provides word alignment and speaker diarization. Its documented limitations include unaligned words, overlapping speech and imperfect diarization; speaker models also require setup/access. | Accurate word-level cleanup is feasible as an optional quality path. Diarization identifies speakers in audio; it does not locate their faces in video. Keep this optional for small machines. |
| [YouTube videos.insert](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert) | Supports media upload, metadata and privacy selection with the `youtube.upload` OAuth scope, including resumable-upload examples. | Official YouTube publishing is feasible. Keep it opt-in and set privacy explicitly; never assume an upload is private by default. Current quota/account behavior should be checked during integration rather than copied from older tutorials. |

Competitor pages establish advertised capabilities, not independent evidence that their clips are more accurate, faster, or more likely to go viral. The Google structured-output documentation redirected to a client-rendered page without readable guidance in this research pass; no schema-support claim is based on that fetch.

**Fit with the current code**

- Topic clipping reaches `hooky/web/server.py` request validation, `hooky/web/analyzer.py` transcript selection/prompts, the existing `hooky/core/gemini.py` retry path, and Studio's results/search controls. Prepared source caching should remain independent of the query; a separate selection checkpoint must include query, model and source revision. Retain a local keyword fallback when Gemini is unavailable.
- Caption editing and projects reach validated project storage, the transcript/result API, Studio's dialogue view, and `hooky/web/rendering.py`'s cue input. Today the single-clip endpoint ignores edited cue payloads, so editing the displayed text alone would not change an export. Original prepared transcripts must remain reusable and separate from user edits.
- Export packs reach `hooky/web/rendering.py`, caption serialization, the output/download routes and gallery. The current ZIP route collects MP4s; it does not provide timed subtitle sidecars. Generated filenames and paths must follow existing output containment checks.
- Selected batch export can reuse the shared render path and per-clip partial outputs. `make_url` already renders multiple automatically selected hooks; the new part is user-selected ranges, durable selections and batch recovery within existing worker limits.
- Crop/output changes reach `hooky/core/utils.py`, `hooky/core/cutter.py`, preview geometry, settings validation, render signatures and media checks. The current renderer assumes a 1080×1920 canvas; switching dimensions must also update caption placement and encoder constraints.
- Pause cleanup can draw on the parts renderer's fragment/concat approach, but its current timed caption behavior must be preserved when cuts collapse the source timeline. Never overwrite source media to apply edits.

Studio already restores one active browser session, selected analysis, clip timing edits and style/framing through `localStorage`. Named projects should add multiple durable projects and portable edited transcripts, rather than duplicate that existing restoration. The CLI already accepts local files and has publishing checkpoints; dedicated local-file playback in the analysis/editor would still require new Studio source handling.

**Recommended build order**

1. Add export packs for a contained, useful addition; then build editable captions and durable project storage together.
2. Add topic/instruction clipping on those transcripts, with evidence shown beside each result. This is the strongest distinguishing feature in the shortlist.
3. Add selected batch export and manual crop controls so creators can finish several usable clips in one session.
4. Evaluate pause cleanup, translated variants and optional YouTube publishing after testing the core edit/export workflow with representative recordings.
5. Keep automatic speaker tracking and dubbing separate until hardware, model/setup costs and output quality are established.

Before presenting ranking scores as predicted success, improve their explanation: the current local fallback assigns percentage scores by rank, not measured retention. Label them as heuristic recommendations and show why the source was selected. Actual audience performance would require an opt-in publishing/analytics integration and a separate evaluation dataset.

Skipped implementation, a UI redesign, new dependencies, cloud storage, stock B-roll, dubbing and a Rust rewrite. No runtime code changed for this research update, so tests were not rerun. Paid competitor products, OAuth-connected uploads, transcription models and GPU workloads were not exercised. Feature value still needs feedback from creators using their own recordings.
