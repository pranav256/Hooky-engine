"""Private process entry point; settings arrive in the environment before imports."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from hooky.web.jobs import EVENT_PREFIX


async def run(request: dict) -> int:
    job, payload = request["job"], request["payload"]
    from hooky.config import OUTPUT_DIR, PROJECT_ROOT
    cache = OUTPUT_DIR / "web_cache" / job["id"]
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["HOOKY_WEB_CACHE_DIR"] = str(cache)

    async def publish():
        while True:
            print(EVENT_PREFIX + json.dumps({key: value for key, value in job.items() if key not in {"logs", "request", "output_root"}}, ensure_ascii=False, allow_nan=False), flush=True)
            await asyncio.sleep(0.2)

    publisher = asyncio.create_task(publish())
    try:
        if job["type"] == "analyze":
            from hooky.web.analyzer import run_analysis_pipeline
            job["result"] = await run_analysis_pipeline(payload["url"], job)
        elif job["type"] == "render_clip":
            from hooky.web.rendering import render_clip
            output = await render_clip(payload, job, cache)
            job.update(status="completed", output_file=output, output_files=[output])
        elif payload.get("target", "").startswith(("http://", "https://")):
            from hooky.web.rendering import make_url
            outputs = await make_url(payload, job, cache)
            job.update(status="completed", output_file=outputs[0], output_files=outputs)
        else:
            from hooky.config import LINKS_FILE, CHANNELS_FILE
            target = payload.get("target", "")
            links = cache / "links.txt"
            channels = cache / "channels.txt"
            if target.startswith("@"):
                channels.write_text(target + "\n", encoding="utf-8")
                links.write_text("", encoding="utf-8")
            elif target:
                links.write_text(target + "\n", encoding="utf-8")
                channels.write_text("", encoding="utf-8")
            else:
                # Preserve the CLI queue's DONE markers and normal retry behavior.
                links, channels = LINKS_FILE, CHANNELS_FILE
            owned_output = OUTPUT_DIR / job["id"]
            environment = {**os.environ, "LINKS_FILE": str(links), "CHANNELS_FILE": str(channels), "OUTPUT_DIR": str(owned_output)}
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-m", "hooky", "make", "--links", str(links),
                "--style", payload["style"], "--framing", payload["framing"], "--no-post", "--no-upload",
                cwd=str(PROJECT_ROOT), env=environment,
            )
            code = await process.wait()
            outputs = [str(file.relative_to(OUTPUT_DIR)).replace("\\", "/") for file in owned_output.rglob("reel*.mp4")
                       if not any(part in {"web_cache", "downloads", ".checkpoints"} for part in file.relative_to(owned_output).parts) and file.stat().st_size > 1000]
            if outputs:
                job.update(output_file=outputs[0], output_files=outputs)
            if code != 0 or not outputs:
                raise RuntimeError(f"Pipeline exited with code {code}; no new complete reels were produced." if not outputs else f"Pipeline exited with code {code}.")
            job.update(status="completed", output_file=outputs[0], output_files=outputs)
        if job.get("status") == "completed":
            job.update(progress=100, step="Analysis complete." if job["type"] == "analyze" else "Reels ready in Output Gallery.")
        return 0 if job.get("status") == "completed" else 1
    except Exception as exc:
        job.update(status="failed", error=str(exc), step="Job failed; review the error and retry.")
        print(f"[worker] {exc}", flush=True)
        return 1
    finally:
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        print(EVENT_PREFIX + json.dumps({key: value for key, value in job.items() if key not in {"logs", "request", "output_root"}}, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(json.load(sys.stdin))))
