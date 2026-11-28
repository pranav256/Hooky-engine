<p align="center">
  <img src="hooky/web/static/favicon.svg" width="64" height="64" alt="Hooky logo">
</p>

<h1 align="center">Hooky</h1>

<p align="center">
  <strong>Find the good parts. Make them shorts.</strong><br>
  AI-assisted video clipping, animated captions, and a local Studio for the final cut.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10 and newer">
  <img src="https://img.shields.io/badge/AI-Google%20Gemini-8E75B2" alt="Google Gemini analysis">
  <img src="https://img.shields.io/badge/Video-FFmpeg-007808?logo=ffmpeg&logoColor=white" alt="FFmpeg video rendering">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="MIT license">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &middot;
  <a href="#your-first-clip">Your first clip</a> &middot;
  <a href="https://hoooky.vercel.app/docs">Documentation</a>
</p>

![Hooky Studio with source input, moment selection, and a portrait preview](docs/images/engine_studio.png)

Bring a podcast, interview, lesson, or local video. Hooky helps you find a moment, tighten the timing, frame it for a phone, and export a captioned 9:16 MP4. Use the browser when you want to edit each clip, or the CLI when you have a batch ready to go.

- **Find a starting point.** Ask Gemini for narrative hooks or search the transcript for a topic such as `startup mistakes`.
- **Make the cut yours.** Adjust boundaries, move the portrait crop, correct captions, and render a quick draft before the final export.
- **Keep a series moving.** Discover videos from channel lists, process saved links, or turn a longer story into numbered parts.
- **Pick the caption mood.** Bold word highlights, a quieter subtitle line, or a serif treatment; choose a preset and preview it.
- **Come back later.** Save Studio projects and resume supported CLI workflows from checkpoints.

Studio, transcription, and rendering run on your machine. Gemini analysis uses Google's API with your key. Hook suggestions are editorial starting points; review the source dialogue and final clip before sharing.

## Quick start

Have Git, Python 3.10 or newer, and a [Gemini API key](https://aistudio.google.com/app/apikey) ready. On first run, the platform launcher creates the Python environment with [uv](https://github.com/astral-sh/uv). It can install FFmpeg/FFprobe through your system package manager. System installation may need administrator access; macOS needs Homebrew.

### Windows (PowerShell)

```powershell
git clone https://github.com/pranav256/Hooky-engine.git
cd Hooky-engine
Set-ExecutionPolicy -Scope Process Bypass
.\main.ps1
```

### Linux & macOS

```bash
git clone https://github.com/pranav256/Hooky-engine.git
cd Hooky-engine
./main.sh
```

Follow the setup prompt to add your key. The launcher creates `.env` from the example when needed, then opens Studio at **http://127.0.0.1:8000**. On later runs, use the same launcher.

Need to revisit setup? Run `./main.sh setup` or `.\main.ps1 setup`. Run `doctor` to check local tools; it reports a missing key separately and does not make a live Gemini call. After setup, `python main.py` is another way to open Studio.

> **Launcher vs. installed command:** The launchers open Studio with no arguments. The installed `hooky` command with no arguments processes pending entries from `links.txt`; use `hooky web` to open Studio explicitly.

## Your first clip

1. **Bring a source.** Paste a YouTube URL or upload a local video in Studio.
2. **Find a moment.** Choose **Detect hooks**. Add an optional **Clip topic** to look for a particular conversation.
3. **Shape the clip.** Select a moment, check its dialogue and start/end times, then choose a portrait crop or the full picture on a blurred canvas.
4. **Give it a voice on screen.** Pick a caption style and use **Edit captions** to correct individual phrases. **Quick preview** makes a short, low-resolution draft.
5. **Export.** Render one moment or choose **Export selected** for several. Pick up finished MP4s in **Output Gallery** or your configured output folder.

Source captions supply subtitle timing when available; Whisper transcription is the fallback. Topic search belongs to **Detect hooks**, not **Run pipeline**. If Gemini is unavailable during topic search, word-matching results are labeled; no matches produce an empty result.

### Keep the edit

Use **Save project** to keep the source, moments, timing, crop positions, caption corrections, topic, and framing choices. **Save project** updates the open project; **Save as new** keeps a separate copy. Reopen a project to continue without another Gemini analysis.

Projects are explicit saves. Browser-session recovery does not update them automatically. Keep the original source available: a project stores edits and source references, not a copy of the media.

<details>
<summary>More of the workspace: settings and channel radar</summary>

![Hooky settings editor](docs/images/engine_settings.png)

![Hooky channel radar](docs/images/engine_radar.png)

Edit runtime settings in the browser and manage the channels that feed your next batch.

</details>

## Batch workflows

**One video, a few clips:**

```bash
./main.sh make interview.mp4 --clips 3
```

**A list of sources:** add YouTube URLs or local paths to `links.txt`, one per line. Options after `|` apply to that source.

```text
https://www.youtube.com/watch?v=VIDEO_ID | clips=auto | style=hormozi
/path/to/podcast.mp4 | clips=5 | min=20 max=50 | style=yellow
```

**A channel to watch:** add handles or channel URLs to `channels.txt`. Discovery can filter by view count and limit the sources it adds.

```text
@hubermanlab | limit=3 | min_views=250000
https://www.youtube.com/@TheDiaryOfACEO | limit=2 | scan=20
```

```bash
./main.sh preview   # Inspect pending work
./main.sh make      # Discover sources and render clips; does not post
./main.sh open      # Open the output folder
```

On Windows, replace `./main.sh` with `.\main.ps1` in these examples.

### Turn a story into a series

```bash
# Condense the story into chronological parts, with optional part labels
./main.sh parts interview.mp4 --part-seconds 60 --labels

# Select funny moments and export with numeric overlays only
./main.sh number-parts cartoon.mp4
```

Parts preserve source order and export without queuing to Instagram. Story mode uses spoken evidence; number-only mode skips Whisper and subtitles but still uses Gemini. See the [parts guide](hooky/parts/README.md) for options, outputs, and recovery.

### Commands at a glance

| Command | Use it to |
| --- | --- |
| `./main.sh web` | Open the local Studio |
| `./main.sh make` | Scan channels, add sources to `links.txt`, and render a batch |
| `./main.sh preview` | Inspect pending work without downloading or rendering |
| `./main.sh status` | See sources, runtime settings, queue, and output |
| `./main.sh retry` | Retry failed source-processing entries |
| `./main.sh open` | Open your output folder |
| `./main.sh parts <file>` | Export a chronological series of vertical parts |
| `./main.sh styles` | List caption presets |
| `./main.sh setup` | Configure the machine and API key |
| `./main.sh doctor` | Check local tools and configuration |
| `./main.sh doctor youtube <URL>` | Check cookies and probe a specific YouTube source |
| `./main.sh post` | Post queued clips now, with configured gaps between uploads |
| `./main.sh cleanup` | Preview removable artifacts; deletion requires `--apply` |

Instagram publishing is optional. Configure its credentials only if you use it. Scheduled posting uses `POST_HOURS`; the `post` command starts posting immediately. Upload checkpoints and duplicate checks support recovery after interruption.

## Caption styles

Choose the look that fits the footage. Presets use word timing, outlines, and highlights to keep speech readable.

| Preset | Look | Font |
| --- | --- | --- |
| `hormozi` | Bold uppercase phrases with yellow word highlights | Montserrat |
| `clean` | Smaller white text with a soft amber highlight | Arial |
| `yellow` | Bright yellow highlights with a black outline | Montserrat |
| `titanium` / `gold` | Warm champagne highlights and uppercase text | Montserrat |
| `minimal` | A quieter serif line with a subtle shadow | Georgia |
| `emerald` | Uppercase text with an emerald highlight | Consolas |
| `crimson` | Bold text with a ruby highlight | Montserrat |

Set `SUB_STYLE` in `.env`, pass `--sub-style`, or choose a style in Studio. `SUB_FONT` overrides the font; availability on your machine affects the rendered result.

## Configuration

The setup wizard writes `.env`. The posting queue is the list of clips waiting to upload. Use Studio settings or edit the file directly; [.env.example](.env.example) lists the available options.

| Setting | What it controls |
| --- | --- |
| `GEMINI_API_KEY` | Your key for AI selection and analysis |
| `QUALITY` | Encoding/download profile: `highest`, `high`, or `low` |
| `X264_PRESET`, `FFMPEG_THREADS` | Encoding speed and CPU use |
| `WHISPER_MODEL`, `WHISPER_COMPUTE` | Local speech transcription; defaults are `tiny` and `int8` on CPU |
| `SUB_STYLE`, `SUB_LANG`, `SUBTITLE_SOURCE` | Caption appearance, language, and timing source |
| `OUTPUT_DIR`, `STATE_DIR` | Exports and persistent runtime state |
| `CHANNEL_BACKLOG_MAX`, `CHANNEL_SCAN_MAX` | Discovery budget and scan depth |
| `YTDLP_COOKIES_FROM_BROWSER` / `YTDLP_COOKIES_FILE` | Optional YouTube authentication; configure one method |
| `IG_USERNAME`, `IG_PASSWORD` | Optional Instagram login |
| `QUEUE_MIN_GAP_MINUTES`, `QUEUE_MAX_GAP_MINUTES`, `POST_HOURS` | Posting gaps and scheduled posting window |

Saved Studio settings apply to new jobs; running jobs keep their starting values. File values take precedence over inherited environment values. Removing a file setting restores the inherited value or built-in default. Source/output path changes apply immediately; changes to the Studio access key or saved browser/history locations require a restart. Completed jobs retain their recorded output directory.

## Studio reference

<details>
<summary>Local uploads, captions, and saved projects</summary>

- Local uploads are limited to 2 GiB, checked with FFprobe, and stored under `STATE_DIR/web_sources`. Delete referencing projects before choosing **Forget local video**. This storage is not a backup.
- Caption corrections update the simulator and exported subtitles, leaving the original transcript unchanged. Export snapshots the edits when it starts. A changed source caption track can invalidate an edit; analyze again before export.
- Projects are versioned JSON under `STATE_DIR/web_projects` (default `.hooky-state/web_projects`) and survive browser changes, restarts, and job-history cleanup. Reload Studio to refresh saves from another browser.
- Stale saves are rejected to protect another tab's work. Use **Save as new** to preserve your version. Unsaved changes prompt before opening another project or analyzing a new source.
- Projects contain transcripts and source URLs, without cloud sync or copied media. Each snapshot allows up to 1 MiB, 100 moments, and 200 caption phrases per moment. Export needs an available source and valid caption timing.

</details>

<details>
<summary>Jobs, cancellation, and storage cleanup</summary>

- Studio runs at most two workers. Cancel stops the worker's download/encoding process tree.
- Job history survives page reloads. After a server restart, unfinished jobs are marked interrupted and need an explicit new start.
- Finished clips remain available when another clip in a batch fails; retry targets unfinished clips.
- History keeps the newest 100 terminal jobs and 200 bounded log lines per job.
- Disposable caches are cleaned after seven days or above 1 GiB when no worker uses them. Completed exports and CLI manifests/checkpoints stay outside this cleanup.
- Run one Studio server process. Project revisions protect concurrent browser tabs; multiple server workers would need an interprocess write lock.

</details>

<details>
<summary>Analysis and transcript caching</summary>

Studio reuses saved video details and transcripts across analysis and export jobs for up to 24 hours. Source URL, caption/transcription language, subtitle source, Whisper model/compute mode, and download authentication settings separate saved entries. Empty or damaged entries are recomputed.

Requests for video details and captions run together when no saved result exists. Unchanged worker status does not trigger another history write. Reusing those results preserves encoding quality and exact clip boundaries.

</details>

## Troubleshooting

| If you see | Try this |
| --- | --- |
| Missing tools or an environment error | Run `doctor`, fix the reported dependency, then rerun the launcher |
| Missing or rejected Gemini key | Run `setup`; `doctor live` makes an explicit live API check |
| YouTube authentication or access errors | Configure one cookie method in `.env`, then run `doctor youtube <URL>` |
| Slow CPU exports | Choose a lighter quality/preset in setup; use **Quick preview** to check an edit first |
| A project that cannot export | Confirm its source still exists and reanalyze if caption timing changed |

## Development

```bash
uv sync --locked --extra all --extra dev
uv run pytest
```

The offline suite covers pipeline logic, settings, file safety, and Studio process cleanup with temporary state and mocked services. CI runs on Ubuntu and Windows.

Optional local-media checks: `uv run pytest --run-integration`. Enable the Studio synthetic media smoke with `HOOKY_MEDIA_SMOKE=1` (PowerShell: `$env:HOOKY_MEDIA_SMOKE='1'`). These checks use FFmpeg/FFprobe and do not call YouTube, Gemini, or Instagram. Browser workflows live in the [sibling website repository](https://github.com/pranav256/Hooky-website); its README explains the isolated hosts and paired CI revisions.

## Contributing

Found a rough edge or have a useful improvement? Open an issue with the command, expected result, and relevant logs with credentials removed. For a pull request, keep the change focused and include the smallest check that demonstrates it works.

## License

[MIT](LICENSE). Make something worth watching.
