# Hooky — AI Video Clipper & Shorts Generator

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12-blue?logo=python&logoColor=white" alt="Python Versions">
  <img src="https://img.shields.io/badge/AI-Google%20Gemini%202.0-8E75B2?logo=google&logoColor=white" alt="Gemini AI">
  <img src="https://img.shields.io/badge/Subtitles-Faster--Whisper-orange" alt="Faster Whisper">
  <img src="https://img.shields.io/badge/Engine-FFmpeg%209%3A16-red?logo=ffmpeg&logoColor=white" alt="FFmpeg">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/Tests-260%20passing-brightgreen" alt="Tests">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
</p>

<p align="center">
  CLI tool and local web studio for extracting vertical 9:16 clips from long-form video with synchronized subtitle animation.
</p>

---

## 🌟 Why Hooky?

Hooky provides automated clipping, transcription, and subtitle styling for vertical video production.

* **Highlight Detection**: Uses Google Gemini to analyze transcripts, detect narrative hooks, and pick high-retention moments (not just audio peaks).
* **Animated Captions**: Powered by `faster-whisper` and source captions with karaoke-style highlight words, custom fonts, and multiple preset styles (`hormozi`, `clean`, `yellow`).
* **Channel Monitor**: Simply drop channel handles in `channels.txt` — Hooky discovers high-performing winners based on view count and feeds them directly to the render pipeline.
* **Scheduled Publishing**: Built-in scheduler with safe pacing, cooldown jitter, upload checkpoints, and duplicate prevention to protect account health.
* **File-Based Configuration**: Zero SQL or heavy docker setup. Driven cleanly by two text files (`channels.txt` and `links.txt`).
* **Hardware Acceleration**: Leverages GPU acceleration (CUDA) and multi-threaded FFmpeg presets with automatic memory/CPU profiling.

---

## 🔄 How It Works

```text
  ┌──────────────────────────────────────────────────────────────┐
  │ 1. SOURCE DISCOVERY                                          │
  │    channels.txt ──> Discover top-viewed YouTube videos        │
  │    links.txt    ──> Direct YouTube URLs or local MP4 files    │
  └───────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 2. AI INTELLIGENCE & HOOK EXTRACTION                         │
  │    Google Gemini 2.0 ──> Identifies punchy, high-retention  │
  │                          viral clips (15s - 60s)             │
  └───────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 3. TRANSCRIPTION & ANIMATED SUBTITLES                        │
  │    faster-whisper ──> Word-level timing & animated styling    │
  │                       (Hormozi, Clean, Yellow presets)       │
  └───────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 4. 9:16 VERTICAL FFmpeg ENCODING                             │
  │    Center-crop, 1080x1920 HD, burned subtitles, covers       │
  └───────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 5. QUEUE & AUTOMATED PUBLISHING                              │
  │    Safe rate-limited Instagram Reels queue with jitter       │
  └──────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### Prerequisites
- Python 3.10 or higher
- [uv](https://github.com/astral-sh/uv) (fastest Python package manager; automatically installed by setup if missing)
- Google Gemini API Key ([get a free key here](https://aistudio.google.com/app/apikey))

### Linux & macOS

```bash
git clone https://github.com/pranav256/Hooky-engine.git
cd Hooky-engine
./main.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/pranav256/Hooky-engine.git
cd Hooky-engine
Set-ExecutionPolicy -Scope Process Bypass
.\main.ps1
```

> **⚡ Single-Command Magic:** Running `main.ps1` (or `./main.sh` / `python main.py`) automatically verifies FFmpeg & `uv`, syncs locked dependencies, initializes `.env`, and launches the visual Web Studio, immediately opening your browser to `http://127.0.0.1:8000`!

---

## 🖥️ Web Studio & Interactive Portal

Hooky includes a built-in visual Web Studio and control portal running locally at `http://127.0.0.1:8000`:

<p align="center">
  <img src="docs/images/engine_studio.png" alt="Hooky Web Studio Interface" width="100%">
</p>

<p align="center">
  <img src="docs/images/engine_settings.png" alt="Hooky Settings Editor" width="49.5%">
  <img src="docs/images/engine_radar.png" alt="Hooky Channel Radar" width="49.5%">
</p>

* **AI Hook Candidate Inspector**: Inspect Gemini-detected viral moments with dialogue transcripts, retention scores, and boundary timecodes.
* **Dual-Mode Visual Simulator**: Switch between the original horizontal video and the 9:16 vertical crop preview.
* **Framing & Subtitles**: Choose a centered portrait crop or fit the full source on an ambient blurred canvas; analysis suggests a mode from source dialogue/title.
* **Channel Radar & `.env` Management**: Monitor creator channels in `channels.txt` and fine-tune runtime environment variables directly in your browser.

---

## ⚡ Workflow in 2 Minutes

After installation, run `./main.sh doctor` (Windows: `.\main.ps1 doctor`). Doctor checks local tools and reports a missing API key separately; it does not call Gemini automatically. Run `./main.sh setup` / `.\main.ps1 setup` to configure the key, then `./main.sh web` / `.\main.ps1 web`.

In Studio, paste a source URL, detect hooks, select a moment, check its start/end and framing, and export. Retrieve the finished file from Output Gallery. Source captions provide subtitle timing; audio transcription is the fallback when a usable track is unavailable. URL make ranks speech scenes locally, while Detect Hooks also asks Gemini for narrative selection.

Studio starts at most two workers at once. Cancel stops that worker's download/encoding process tree. Jobs survive page reload in history; after a server restart unfinished jobs are marked interrupted and require an explicit new start. Saved settings apply to new jobs; active jobs keep their starting settings. Studio file values take precedence over the inherited process environment. Removing a file setting restores the inherited value or built-in default. Source/output path changes apply immediately; token and history/session storage changes require a restart. Previously completed jobs retain access to their recorded output directory.

Studio keeps the newest 100 terminal jobs and 200 bounded log lines per job. Disposable Studio caches are cleaned after seven days or above 1 GiB, when no worker is using them. Completed exports and CLI manifests/checkpoints stay outside this cleanup.

Studio reuses prepared video details and transcripts across analysis and export jobs for up to 24 hours. Source URL, caption/transcription language, subtitle source, Whisper model/compute mode, and download authentication settings separate cache entries. Empty or damaged entries are recomputed. Metadata and caption requests run together on a cache miss; unchanged worker status no longer triggers repeated history writes. Encoding quality and exact clip boundaries are preserved. See [performance findings and feature priorities](docs/performance-and-features.md).

To find specific spoken content, enter an optional **Clip topic** (for example, `startup mistakes`) and choose **Detect hooks**. Hooky reads the full transcript in bounded chunks, shows the matching source dialogue and reasons, and returns up to four non-overlapping clips. If Gemini is unavailable, it labels word-matching results; no matches produce a clear empty state. Leave the topic blank for the existing general analysis. Topic search applies to Detect hooks, not Run pipeline.

Select a moment to **Edit captions**. Search within its phrases, correct text, or reset to the original. Edits update the caption simulator and the exported subtitles, and are restored with the current Studio session in this browser. Export snapshots its edits when it starts. Original source transcripts stay unchanged. A changed source caption track can invalidate an old edit; analyze again before exporting.

Use **Projects** to keep an analyzed source for later. Enter a name and choose **Save project** to save all its moments, selected clip, timing, caption corrections, topic and framing/subtitle choices. **Save project** updates the opened project and its name; **Save as new** keeps a separate copy. Choose a saved project and **Open project** to reopen its moments without another Gemini analysis. Unsaved changes prompt before opening another project or starting a new analysis. Saves are explicit; browser-session restoration does not automatically update the project on disk.

Projects are versioned JSON files under `STATE_DIR/web_projects` (by default `.hooky-state/web_projects`). They survive browser changes, restarts and job-history cleanup. Reload Studio to refresh the list after saving from another browser. Stale saves are rejected so another tab's work stays intact; **Save as new** preserves your version. Project files include transcripts and source URLs, but do not copy the source media or provide cloud sync. Export still needs an available source and valid caption timing. Each snapshot is limited to 1 MiB, 100 moments and 200 caption phrases per moment.

### 1. Feed Channels or Links

Add YouTube handles to `channels.txt`:
```text
@hubermanlab | limit=3 | min_views=250000
https://www.youtube.com/@TheDiaryOfACEO | limit=2 | scan=20
```

Or add specific video URLs / local files to `links.txt`:
```text
https://www.youtube.com/watch?v=dQw4w9WgXcQ | clips=auto | style=hormozi
/path/to/podcast.mp4 | clips=5 | min=20 max=50 | style=yellow
```

### 2. Run the Pipeline or Web Studio

```bash
# Launch the visual Web Studio & Control Portal in your browser (http://127.0.0.1:8000)
./main.sh web

# Or run terminal automation:
./main.sh make
```

```bash
# Verify your installation & dependencies
./main.sh doctor

# Preview what will be processed without downloading or rendering
./main.sh preview

# Fetch winning channel videos, extract highlights, and render clips
./main.sh make

# Inspect generated clips in your file explorer
./main.sh open

# Post queued clips safely to Instagram (optional)
./main.sh post
```

*(On Windows, replace `./main.sh` with `.\main.ps1`)*

---

## 🎨 Subtitle Styles

Hooky includes high-converting subtitle templates inspired by top creators:

| Style | Description | Preview / Font |
|---|---|---|
| `titanium` / `gold` | **Quiet Luxury**: Warm titanium metallic glow, elegant editorial typography | Montserrat Bold |
| `hormozi` | **Viral Hook Style**: Bold font, uppercase, dynamic bright color highlights on spoken words | Montserrat Bold |
| `clean` | **Modern Minimalist**: Subtle background box, legible, professional podcast aesthetic | Arial |
| `yellow` | **High-Contrast Attention**: Vibrant yellow word highlights for maximum retention | Montserrat Bold |
| `minimal` | **Cinematic Italic**: Clean underline without background boxes | Georgia |
| `emerald` | **Code & Tech**: High-tech matrix emerald green glow | Consolas |
| `crimson` | **Bold Shock**: Punchy crimson neon drop-shadow | Montserrat Bold |

List all available styles:
```bash
./main.sh styles
```

---

## 🛠️ CLI Command Reference

| Command | Action |
|---|---|
| `./main.sh web` (or `./main.sh`) | Launch visual Web Studio and Control Portal in your browser (`http://127.0.0.1:8000`) |
| `./main.sh make` | Scan channels, add winners to `links.txt`, and render all clips |
| `./main.sh preview` | Dry-run: view pending jobs and highlights without touching files |
| `./main.sh post` | Drain the Instagram queue with safety cooldown intervals |
| `./main.sh status` | Show current queue state, render progress, and recent errors |
| `./main.sh retry` | Re-attempt failed links or rendering tasks |
| `./main.sh cleanup` | Preview and safely remove raw source files after posting |
| `./main.sh parts <file>` | Split long videos into sequential episodes/parts |
| `./main.sh doctor` | Diagnostic health check (FFmpeg, Gemini key, yt-dlp, cookies) |
| `./main.sh doctor youtube` | Verify YouTube cookies and test bypass on age-gated videos |

---

## ⚙️ Configuration (`.env`)

The setup wizard generates `.env`. You can fine-tune settings anytime:

```dotenv
# --- AI Engine ---
GEMINI_API_KEY=AIzaSy...               # Required for highlight extraction
GEMINI_MODEL=gemini-3.8-flash           # Current default model

# --- Subtitles & Whisper ---
WHISPER_MODEL=tiny                     # Default; larger installed models are optional
WHISPER_COMPUTE=int8                   # int8, float16 (for GPU CUDA)
SUB_STYLE=hormozi                      # Default; list presets with ./main.sh styles

# --- Video Quality & Encoding ---
QUALITY=highest                        # highest, high, low
X264_PRESET=medium                     # ultrafast, veryfast, medium, slow
FFMPEG_THREADS=2

# --- Channel Discovery ---
CHANNEL_BACKLOG_MAX=20                 # Default pending source budget
CHANNEL_SCAN_MAX=0                     # Default: inspect all available videos

# --- Instagram Automation (Optional) ---
IG_USERNAME=your_account
IG_PASSWORD=your_password
QUEUE_MIN_GAP_MINUTES=60               # Default minimum between scheduled posts
QUEUE_MAX_GAP_MINUTES=120              # Default maximum gap between scheduled posts
POST_HOURS=17-0                        # Default local schedule: 5 PM to midnight

# --- YouTube Auth / Cookies (Optional for 403 bypass) ---
YTDLP_COOKIES_FROM_BROWSER=chrome      # chrome, firefox, edge, or brave
# or
YTDLP_COOKIES_FILE=/path/to/cookies.txt
```

---

## 🧩 Split Full Videos into "Part 1, Part 2..."

Need to split a long masterclass or movie clip into sequential parts?

```bash
# Auto-split into clean numbered 60s segments with part badges
./main.sh parts interview.mp4 --part-seconds 60
```

---

## 🧪 Testing & Code Quality

The offline test suite covers pipeline logic, file safety, settings, and Studio process cleanup. CI runs on Ubuntu and Windows:

```bash
# Sync dependencies
uv sync --extra all --extra dev

# Run test suite
uv run pytest
```

---

## 🤝 Contributing

Contributions, feature requests, and bug reports are welcome!
1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'feat: Add AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.

---

<p align="center">
  <sub>Built for content creators who value speed, quality, and complete ownership of their pipeline.</sub>
</p>
