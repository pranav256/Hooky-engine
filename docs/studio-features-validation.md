Topic search and caption editing — implementation review, 14 November 2026.

Studio now accepts an optional topic for Detect hooks, analyzes all transcript chunks, validates returned ranges, filters overlapping candidates, and includes actual source cues with each result. AI failure uses labeled word matching. A successful search with no matches completes without inventing replacement clips. General analysis remains available with a blank topic.

The selected clip's caption phrases can be searched, corrected and reset. Edited text reaches both the caption simulator and FFmpeg subtitle generation. Edits stay separate from the prepared source cache and are saved/restored with the existing browser session. Inputs are bounded and validated before launching a worker, and the renderer rejects edits whose phrase times no longer match the source. Retry preserves the saved export's caption edits. Running exports retain their starting text.

Validation evidence:

- Full Python suite: 634 passed, 8 opt-in integration skips.
- Real FFmpeg smoke suite: 5 passed, including fractional timing, center/fit framing, visible subtitles, edited ASS text, rejection of invalid media, and cancellation before publication.
- New browser check: `Hooky-website/scripts/check-studio-features.mjs`, at 390, 768, 1440 and 1728 pixels. Covers topic forwarding, source evidence, caption search, editing, preview text, Tab navigation, reload restoration, history refresh preserving the latest edits, reset, escaped markup, invalid-caption recovery, export payloads, disabled fields during export, cancellation and no-match states. It runs in the website's browser CI suite.
- Existing Studio browser workflow passes at the same four widths, including job/timing reload, delayed polling/reconnection, output gallery, settings, keyboard focus and geometry.
- Engine wheel and source archive build successfully and pass Twine and wheel-content checks. The wheel's changed Studio assets and feature modules match the working source. The production website build, lint, type validation and Studio path-resolution test pass.
- Better Design guidance loaded before styling and behavior. Scoped source, copy and rendered-spacing reviews were run for the changed controls; review findings were fixed and checked again. These are an editor review, not a dashboard receipt or accessibility certification.

Browser checks use intercepted API fixtures against an isolated local Studio server. They do not claim live Gemini or YouTube integration coverage. Caption preview remains a styling simulator, not a frame-accurate render preview. Topic searches may require several Gemini requests on long recordings. Current-browser session saving can fail if storage is unavailable, and the UI reports that edits are then kept for the session only.

No paid model calls, real account uploads, screen-reader walkthrough or GPU workload was exercised. Export packs, batch selection, new output formats, silence cleanup, translations and speaker tracking remain deferred. Named projects were subsequently added; see `docs/studio-projects-validation.md`.
