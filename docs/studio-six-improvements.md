# Studio improvements

Implementation plan, 25 November 2026.

1. Export selected moments in one bounded worker. Validate every clip before launch, reuse prepared source cues, report each success/failure, retain completed files after failure/cancellation, and retry only failed clips.
2. Add one fixed crop position per moment, with pointer dragging, keyboard sliders and reset. Carry normalized horizontal/vertical positions through projects, requests and the shared FFmpeg filter. Keep center/fit defaults compatible with CLI callers.
3. Render a maximum ten-second 360×640 draft through the same caption/framing path. Keep drafts out of the final gallery and serve them through authenticated job routes. Final exports remain 1080×1920.
4. Replace percentage hook scores with ordered suggestions, selection method, source dialogue and the selection reason. Preserve old project compatibility without displaying scores as predicted outcomes.
5. Upload a local video through an authenticated bounded streaming endpoint. Validate the actual media, store it separately from disposable caches, use an opaque source ID, and reuse Studio analysis/edit/export/projects with native playback. Never accept arbitrary filesystem paths in the new editor APIs.
6. Replace the website's illustrative hero with playable source/export footage, with source/license attribution, captions and an actual Hooky-rendered result.

Scope: engine source preparation, validation/routes, worker/rendering, shared crop filter, project snapshots, Studio HTML/JS/CSS, focused unit/media/browser checks; production website hero, assets, copy, documentation and browser checks. Keep the separate redesign checkout untouched.

Verification: offline engine suite, real FFmpeg tests for crop/draft/local output, browser flows at mobile/desktop sizes, website lint/typecheck/build, source/copy/rendered-spacing review. Use isolated settings/state/output; no user credentials or paid AI requests. Commit and push both repositories after checks pass.
