import time
import io
import zipfile

import pytest
from unittest.mock import AsyncMock, Mock
from fastapi.testclient import TestClient
from hooky.web import server
from hooky.web.server import app, get_web_token

TOKEN = get_web_token()
client = TestClient(app, headers={"X-Hooky-Token": TOKEN})
unauthed_client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_studio(tmp_path, monkeypatch):
    """API checks never change user files or start downloads/media jobs."""
    for name, path in {
        "ENV_FILE": tmp_path / ".env",
        "PROJECT_ROOT": tmp_path,
    }.items():
        monkeypatch.setattr(server, name, path)
    monkeypatch.setattr(server.os, "environ", server.os.environ.copy())
    monkeypatch.setattr(server, "DOTENV_KEYS", set())
    async def fake_start(manager, kind, payload, environment, project_root):
        from uuid import uuid4
        job_id = f"{kind}_{uuid4().hex}"
        job = {"id": job_id, "type": "render_clip" if kind == "render" else kind,
               "status": "running", "progress": 5, "created_at": time.time(),
               "updated_at": time.time(), "logs": [], "log_offset": 0, "request": payload,
               "result": None, "url": payload.get("url", ""), "output_root": environment["OUTPUT_DIR"]}
        manager.jobs[job_id] = job
        return job
    monkeypatch.setattr(server.JobManager, "start", fake_start)
    monkeypatch.setattr(server, "_MANAGER", None)
    monkeypatch.setattr(server, "_STABLE_SETTINGS", {key: "" for key in server._STABLE_SETTINGS})
    for key in ("OUTPUT_DIR", "CHANNELS_FILE", "LINKS_FILE", "STATE_DIR", "IG_SESSION_FILE"):
        server.os.environ.pop(key, None)
    server._JOBS.clear()
    yield
    server._JOBS.clear()


def test_index_endpoint():
    response = unauthed_client.get("/")
    assert response.status_code == 200
    assert "Hooky Studio" in response.text
    # Verify auth token was injected into HTML
    assert TOKEN in response.text
    # Verify auth cookie is set
    assert response.cookies.get("hooky_token") == TOKEN
    # Verify key controls are present
    assert 'id="smart-recommendation-pill"' in response.text
    assert 'id="frame-mode-auto"' in response.text
    assert 'id="frame-mode-fit"' in response.text
    assert 'id="frame-mode-center"' in response.text
    assert 'id="tab-settings"' in response.text
    assert 'id="env-raw-textarea"' in response.text


def test_topic_and_caption_edits_survive_validated_job_requests():
    source = "https://example.com/video"
    response = client.post("/api/analyze/start", json={"url": source, "topic": " startup mistakes "})
    job = server._job_manager().jobs[response.json()["job_id"]]
    assert job["request"]["topic"] == "startup mistakes"
    edits = [{"start": 1, "end": 3, "text": "Correct name"}]
    response = client.post("/api/jobs/render_clip", json={"url": source, "start": 1, "end": 10, "caption_edits": edits})
    assert server._job_manager().jobs[response.json()["job_id"]]["request"]["caption_edits"] == edits


@pytest.mark.parametrize("topic", [None, [], "x" * 501, "null\0byte"])
def test_invalid_topic_never_launches_a_worker(topic):
    response = client.post("/api/analyze/start", json={"url": "https://example.com/video", "topic": topic})
    assert response.status_code == 400
    assert not server._job_manager().jobs


@pytest.mark.parametrize("edits", [None, [{"start": True, "end": 2, "text": "x"}], [{"start": 1, "end": 3, "text": ""}]])
def test_invalid_caption_edits_never_launch_a_worker(edits):
    response = client.post("/api/jobs/render_clip", json={"url": "https://example.com/video", "start": 1, "end": 10, "caption_edits": edits})
    assert response.status_code == 400
    assert not server._job_manager().jobs


def test_index_assets_are_served_locally():
    import re
    fresh_client = TestClient(app)
    index = fresh_client.get("/")
    assets = set(re.findall(r'(?:src|href)="(/static/[^\"]+)"', index.text))
    assert "/static/studio.js" in assets
    for asset in assets:
        response = fresh_client.get(asset)
        assert response.status_code == 200, asset
        assert response.content
    fonts = re.findall(r"url\(['\"]?([^)'\"]+)", fresh_client.get("/static/brand.css").text)
    assert "system-ui" in fresh_client.get("/static/brand.css").text
    for font in fonts:
        assert fresh_client.get(font if font.startswith("/") else "/static/" + font).status_code == 200


@pytest.mark.parametrize("path", ["/", "/static/studio.js", "/api/channels"])
@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "[::1].evil.example", "[::1", "", "localhost:bad"])
def test_every_route_rejects_foreign_or_malformed_hosts(path, host):
    response = client.get(path, headers={"Host": host})
    assert response.status_code == 403


@pytest.mark.parametrize("host", ["LOCALHOST:8000", "127.0.0.1", "[::1]:8000", "[::1]"])
def test_loopback_host_variants_are_allowed(host):
    assert client.get("/static/studio.js", headers={"Host": host}).status_code == 200


def test_static_mount_does_not_expose_project_files():
    for path in ["/static/missing.js", "/static/%2e%2e/server.py", "/static/%2e%2e/%2e%2e/%2e%2e/.env"]:
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/api/channels", "/api/jobs/missing", "/api/outputs/missing.mp4"])
def test_static_access_does_not_bypass_api_auth(path):
    fresh_client = TestClient(app)
    assert fresh_client.get("/static/studio.js").status_code == 200
    assert fresh_client.get(path).status_code == 401


def test_token_auth_enforcement():
    fresh_client = TestClient(app)
    # Without token -> 401
    res = fresh_client.get("/api/doctor")
    assert res.status_code == 401
    assert "Unauthorized" in res.json().get("detail", "")

    # With invalid token -> 401
    bad_res = fresh_client.get("/api/doctor", headers={"X-Hooky-Token": "invalid_bad_token"})
    assert bad_res.status_code == 401

    # With valid token query param -> 200
    query_res = fresh_client.get(f"/api/doctor?token={TOKEN}")
    assert query_res.status_code == 200

    # With valid token header -> 200
    good_res = client.get("/api/doctor")
    assert good_res.status_code == 200

    # With valid token cookie -> 200
    cookie_client = TestClient(app, cookies={"hooky_token": TOKEN})
    cookie_res = cookie_client.get("/api/doctor")
    assert cookie_res.status_code == 200


def test_host_header_validation():
    # Loopback hosts -> allowed
    res_local = client.get("/api/doctor", headers={"Host": "127.0.0.1:8000"})
    assert res_local.status_code == 200

    # Forbidden external host -> 403
    res_bad_host = client.get("/api/doctor", headers={"Host": "evil.example.com"})
    assert res_bad_host.status_code == 403


def test_doctor_endpoint():
    response = client.get("/api/doctor")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "checks" in data
    assert "gpu" in data


def test_channels_endpoint():
    response = client.get("/api/channels")
    assert response.status_code == 200
    data = response.json()
    assert "channels" in data


def test_outputs_endpoint():
    response = client.get("/api/outputs")
    assert response.status_code == 200
    data = response.json()
    assert "files" in data


def test_download_all_keeps_finished_clips_and_cleans_archive(monkeypatch):
    current = server.PROJECT_ROOT / "output"
    old = server.PROJECT_ROOT / "old-output"
    for root in (current, old):
        (root / "session").mkdir(parents=True)
        (root / "session/reel.mp4").write_bytes(b"video" * 400)
        (root / "session/reel.tmp.mp4").write_bytes(b"unfinished" * 400)
        (root / "session/snippet_source.mp4").write_bytes(b"source" * 400)
    server._job_manager().jobs["saved"] = {"id": "saved", "output_root": str(old), "output_file": "session/reel.mp4"}
    temporary = server.tempfile.NamedTemporaryFile
    created = []
    def track_archive(**kwargs):
        file = temporary(dir=server.PROJECT_ROOT, **kwargs)
        created.append(file.name)
        return file
    monkeypatch.setattr(server.tempfile, "NamedTemporaryFile", track_archive)
    assert TestClient(app).get("/api/outputs/download-all").status_code == 401
    response = client.get("/api/outputs/download-all")
    assert response.status_code == 200
    assert 'filename="hooky-clips.zip"' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert bundle.namelist() == ["output-1/session/reel.mp4", "output-2/session/reel.mp4"]
        assert all(bundle.read(name) == b"video" * 400 for name in bundle.namelist())
    assert created and not any(server.Path(path).exists() for path in created)
    monkeypatch.setattr(server.zipfile.ZipFile, "write", Mock(side_effect=OSError("Disk full")))
    with pytest.raises(OSError, match="Disk full"):
        client.get("/api/outputs/download-all")
    assert not any(server.Path(path).exists() for path in created)


def test_output_folder_only_opens_trusted_current_or_historical_clips(monkeypatch):
    root = server.PROJECT_ROOT / "previous"
    (root / "session").mkdir(parents=True)
    (root / "session/reel.mp4").write_bytes(b"video" * 400)
    server._job_manager().jobs["saved"] = {"id": "saved", "output_root": str(root), "output_file": "session/reel.mp4"}
    opener = Mock(return_value=True)
    monkeypatch.setattr(server, "open_path", opener)
    endpoint = "/api/outputs/open-folder"
    assert TestClient(app).post(endpoint).status_code == 401
    assert client.post(endpoint, params={"job_id": "saved", "filepath": "session/reel.mp4"}).status_code == 200
    opener.assert_called_once_with(root / "session")
    for filepath in ("../outside.mp4", str(server.PROJECT_ROOT / "outside.mp4"), "session/reel.tmp.mp4", ".env"):
        assert client.post(endpoint, params={"job_id": "saved", "filepath": filepath}).status_code == 404
    assert opener.call_count == 1
    assert client.post(endpoint, params={"job_id": "missing"}).status_code == 404
    opener.return_value = False
    assert client.post(endpoint, params={"job_id": "saved"}).status_code == 500


def test_download_all_empty_gallery():
    assert client.get("/api/outputs/download-all").status_code == 404


def test_env_roundtrip():
    res = client.get("/api/env")
    assert res.status_code == 200
    initial = res.json()
    assert "raw" in initial
    assert "values" in initial

    test_key = "HOOKY_TEST_VAR"
    test_val = "verified_12345"
    payload = {"values": {test_key: test_val}}
    save_res = client.post("/api/env", json=payload)
    assert save_res.status_code == 200

    res2 = client.get("/api/env")
    assert res2.status_code == 200
    assert res2.json()["values"].get(test_key) == test_val

    raw_clean = "\n".join([line for line in res2.json()["raw"].splitlines() if not line.startswith(test_key)])
    client.post("/api/env", json={"raw": raw_clean})


def test_render_validation():
    # Empty url -> 400
    res1 = client.post("/api/jobs/render_clip", json={"url": ""})
    assert res1.status_code == 400

    # Malicious or flag url -> 400
    res_flag = client.post("/api/jobs/render_clip", json={"url": "--exec rm -rf /"})
    assert res_flag.status_code == 400

    # Invalid timestamps: end <= start -> 400
    res_time = client.post("/api/jobs/render_clip", json={
        "url": "https://www.youtube.com/watch?v=EDCwQe7P8T0",
        "start": 50,
        "end": 20,
    })
    assert res_time.status_code == 400

    # Valid render job -> 200
    res_ok = client.post("/api/render", json={
        "url": "https://www.youtube.com/watch?v=EDCwQe7P8T0",
        "start": 10,
        "end": 20,
    })
    assert res_ok.status_code == 200
    assert "job_id" in res_ok.json()


def test_make_target_validation():
    # Injection attempt in target with leading '-' -> 400
    res_bad = client.post("/api/make", json={"target": "--custom-malicious-arg"})
    assert res_bad.status_code == 400


def test_cors_and_password_masking():
    # 1. Foreign origin should not receive Access-Control-Allow-Origin
    res_cors = client.get("/api/env", headers={"Origin": "http://evil.example"})
    assert res_cors.headers.get("access-control-allow-origin") is None or res_cors.headers.get("access-control-allow-origin") != "http://evil.example"

    # 2. Local origin should be accepted
    res_local = client.get("/api/env", headers={"Origin": "http://localhost:8000"})
    assert res_local.headers.get("access-control-allow-origin") == "http://localhost:8000"

    # 3. Simple text/plain post without json content-type must be rejected with 415
    res_bad_ct = client.post("/api/env", content="RAW_TEST=1", headers={"Content-Type": "text/plain"})
    assert res_bad_ct.status_code == 415

    # 4. Sensitive credentials masking verification
    res_env = client.get("/api/env")
    assert res_env.status_code == 200
    values = res_env.json().get("values", {})
    if "IG_PASSWORD" in values and values["IG_PASSWORD"]:
        assert values["IG_PASSWORD"] == "••••••••"
    if "GEMINI_API_KEY" in values and values["GEMINI_API_KEY"]:
        assert values["GEMINI_API_KEY"] == "••••••••"


def test_outputs_foldering_and_security():
    res = client.get("/api/outputs")
    assert res.status_code == 200
    data = res.json()
    assert "folders" in data
    assert "files" in data
    assert "count" in data

    # Ensure no internal snippets or downloads leaked into outputs
    for f in data["files"]:
        assert not f["name"].startswith("snippet_"), f"Snippet leaked into outputs: {f['name']}"
        assert not f["name"].endswith(".tmp.mp4"), f"Temp file leaked into outputs: {f['name']}"
        assert "web_cache" not in f["rel_path"], f"web_cache file leaked into outputs: {f['rel_path']}"
        assert "downloads" not in f["rel_path"], f"downloads file leaked into outputs: {f['rel_path']}"

    # Path traversal attack on serve_output_file must be rejected or 404
    bad_traversal = client.get("/api/outputs/../../pyproject.toml")
    assert bad_traversal.status_code in (403, 404)


@pytest.mark.parametrize("field,path_name", [("channels", "CHANNELS_FILE"), ("links", "LINKS_FILE")])
@pytest.mark.parametrize("payload", [{}, [], None, {"wrong": []}, "invalid"])
def test_source_list_invalid_payload_preserves_file(field, path_name, payload):
    path = server._configured_path(path_name)
    path.write_text("original\n", encoding="utf-8")
    response = client.post(f"/api/{field}", content=server.json.dumps(payload), headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == "original\n"


@pytest.mark.parametrize("field,path_name", [("channels", "CHANNELS_FILE"), ("links", "LINKS_FILE")])
@pytest.mark.parametrize("value", ["not-a-list", [None], [42], ["first\nsecond"], ["first\rsecond"], ["nul\0"]])
def test_source_list_rejects_invalid_lines(field, path_name, value):
    path = server._configured_path(path_name)
    path.write_text("original\n", encoding="utf-8")
    response = client.post(f"/api/{field}", json={field: value})
    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == "original\n"


@pytest.mark.parametrize("field,path_name", [("channels", "CHANNELS_FILE"), ("links", "LINKS_FILE")])
def test_source_list_save_creates_parent_and_can_clear(field, path_name, tmp_path):
    path = tmp_path / "nested" / f"{field}.txt"
    server.os.environ[path_name] = str(path)
    response = client.post(f"/api/{field}", json={field: [" @creator ", "", "  ", "second"]})
    assert response.json() == {"status": "saved", "count": 2}
    assert path.read_text(encoding="utf-8") == "@creator\nsecond\n"
    assert client.post(f"/api/{field}", json={field: []}).status_code == 200
    assert path.read_text(encoding="utf-8") == ""


def test_source_list_failed_replace_preserves_previous_file(monkeypatch):
    path = server._configured_path("CHANNELS_FILE")
    path.write_text("original\n", encoding="utf-8")
    def fail_replace(*args):
        raise OSError("Simulated disk failure")
    monkeypatch.setattr(server.Path, "replace", fail_replace)
    with pytest.raises(OSError):
        server._write_source_lines(path, ["replacement"])
    assert path.read_text(encoding="utf-8") == "original\n"
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("field,value", [("start", "nan"), ("end", "nan"), ("end", "inf"), ("start", "-inf")])
def test_render_rejects_nonfinite_timestamps(field, value):
    payload = {"url": "https://www.youtube.com/watch?v=EDCwQe7P8T0", "start": 10, "end": 20, field: value}
    assert client.post("/api/render", json=payload).status_code == 400
    assert not server._JOBS


@pytest.mark.parametrize("endpoint", ["/api/analyze/start", "/api/analyze", "/api/render"])
@pytest.mark.parametrize("url", [None, 123, [], "--exec something", "file:///tmp/movie.mp4"])
def test_analysis_and_render_reject_invalid_urls(endpoint, url):
    assert client.post(endpoint, json={"url": url}).status_code == 400
    assert not server._JOBS


@pytest.mark.parametrize("endpoint", ["/api/analyze/start", "/api/render", "/api/make"])
def test_same_time_jobs_get_distinct_ids(endpoint, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    payload = {"url": "https://www.youtube.com/watch?v=EDCwQe7P8T0", "start": 10, "end": 20, "video_title": "Sample"}
    if endpoint == "/api/make":
        payload = {"target": payload["url"]}
    first = client.post(endpoint, json=payload)
    second = client.post(endpoint, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["job_id"] != second.json()["job_id"]


def test_malformed_json_is_rejected_without_changes():
    assert client.post("/api/channels", content="{", headers={"Content-Type": "application/json"}).status_code == 400
    assert not server._configured_path("CHANNELS_FILE").exists()


JOB_ENDPOINTS = ["/api/render", "/api/jobs/render", "/api/jobs/render_clip", "/api/make", "/api/jobs/make"]


@pytest.mark.parametrize("endpoint", JOB_ENDPOINTS)
@pytest.mark.parametrize("body", ["{", "[]", "null", "42", '"text"'])
def test_job_body_must_be_a_json_object(endpoint, body):
    response = client.post(endpoint, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert not server._JOBS
    assert not (server.PROJECT_ROOT / ".hooky-state").exists()


@pytest.mark.parametrize("endpoint", JOB_ENDPOINTS)
@pytest.mark.parametrize("field,value", [
    ("style", None), ("style", []), ("style", 123), ("style", "unknown"), ("style", ""),
    ("framing", None), ("framing", {}), ("framing", True), ("framing", "unknown"),
])
def test_job_options_are_validated_before_start(endpoint, field, value):
    payload = {"url": "https://example.com/video", "target": "https://example.com/video", "start": 0, "end": 10, field: value}
    assert client.post(endpoint, json=payload).status_code == 400
    assert not server._JOBS
    assert not (server.PROJECT_ROOT / ".hooky-state").exists()


@pytest.mark.parametrize("field", ["title", "video_title", "source_title"])
@pytest.mark.parametrize("value", [None, [], 123, True, "null\0title"])
def test_render_title_types_are_validated_before_launch(field, value, monkeypatch):
    launch = AsyncMock()
    monkeypatch.setattr(server, "_launch", launch)
    payload = {"url": "https://example.com/video", "start": 0, "end": 10, "video_title": "Video", field: value}
    assert client.post("/api/render", json=payload).status_code == 400
    launch.assert_not_called()
    assert not server._JOBS


@pytest.mark.parametrize("field,value", [("start", True), ("end", False), ("start", None), ("end", []), ("end", 10 ** 400)])
def test_render_invalid_numeric_timestamps_do_not_launch_jobs(field, value):
    payload = {"url": "https://example.com/video", "start": 0, "end": 10, field: value}
    assert client.post("/api/render", json=payload).status_code == 400
    assert not server._JOBS


@pytest.mark.parametrize("field", ["target", "url"])
@pytest.mark.parametrize("value", [None, 123, [], {}, True, "https://", "https://example.com/\nsecond", "https://example.com/\0"])
def test_make_invalid_source_does_not_launch_jobs_or_write_target_file(field, value):
    assert client.post("/api/make", json={field: value}).status_code == 400
    assert not server._JOBS
    assert not (server.PROJECT_ROOT / ".hooky-state").exists()


@pytest.mark.parametrize("body", [
    {"raw": 'BAD="unclosed'}, {"raw": "not a setting"}, {"values": {"FFMPEG_THREADS": "nan"}},
    {"values": {"QUEUE_MIN_GAP_MINUTES": "inf"}}, {"values": {"MAKE_COVERS": "maybe"}},
    {"values": {"SUB_STYLE": "fake"}}, {"values": {"IG_PASSWORD": None}},
    {"values": {"VALID_KEY": "line\nbreak"}}, {"values": {"0INVALID": "bad"}},
    {"values": {"CHANNEL_BACKLOG_MAX": ""}}, {"values": {"SOURCE_LONG_THRESHOLD_MINUTES": ""}},
])
def test_invalid_settings_preserve_file(body):
    server.ENV_FILE.write_text("CUSTOM_EXTENSION=unchanged\n", encoding="utf-8")
    before = server.ENV_FILE.read_bytes()
    response = client.post("/api/env", json=body)
    assert response.status_code == 400
    assert server.ENV_FILE.read_bytes() == before


def test_settings_preview_and_mixed_draft_preserve_new_secret_clear_and_extensions():
    server.ENV_FILE.write_text("IG_PASSWORD=old-secret\nGEMINI_API_KEY=old-key\n", encoding="utf-8")
    body = {"draft": [
        {"raw": "IG_PASSWORD=new-secret\nGEMINI_API_KEY=new-key\nCUSTOM=first\n"},
        {"values": {"IG_PASSWORD": server.MASK, "QUALITY": "high"}},
        {"raw": f"IG_PASSWORD={server.MASK}\nGEMINI_API_KEY={server.MASK}\nCUSTOM=second\n"},
        {"values": {"GEMINI_API_KEY": "", "SUB_STYLE": "gold"}},
    ]}
    preview = client.post("/api/env/preview", json=body)
    assert preview.status_code == 200
    assert preview.json()["values"]["IG_PASSWORD"] == server.MASK
    assert "new-secret" not in preview.text
    assert "new-key" not in preview.text
    assert "old-secret" in server.ENV_FILE.read_text(encoding="utf-8")
    saved = client.post("/api/env", json=body)
    assert saved.status_code == 200
    actual = server.parse_dotenv(server.ENV_FILE.read_text(encoding="utf-8"))
    assert actual == {"IG_PASSWORD": "new-secret", "GEMINI_API_KEY": "", "CUSTOM": "second", "SUB_STYLE": "gold"}
    assert "new-secret" not in saved.text
    prior = server.ENV_FILE.read_bytes()
    body["draft"].append({"raw": 'BAD="invalid'})
    assert client.post("/api/env", json=body).status_code == 400
    assert server.ENV_FILE.read_bytes() == prior


def test_settings_exact_masks_all_credentials_and_no_parent_environment_mutation():
    values = {key: f"private-{key}" for key in server.SECRET_KEYS}
    server.ENV_FILE.write_text(server.dump_dotenv(values), encoding="utf-8")
    environment = server.os.environ.copy()
    sanitized = client.get("/api/env").json()
    assert all(sanitized["values"][key] == server.MASK for key in values)
    assert client.post("/api/env", json={"raw": sanitized["raw"]}).status_code == 200
    assert server.parse_dotenv(server.ENV_FILE.read_text(encoding="utf-8")) == values
    assert client.post("/api/env", json={"values": {"IG_PASSWORD": "••new-real-secret", "IG_2FA_CODE": ""}}).status_code == 200
    parsed = server.parse_dotenv(server.ENV_FILE.read_text(encoding="utf-8"))
    assert parsed["IG_PASSWORD"] == "••new-real-secret" and parsed["IG_2FA_CODE"] == ""
    assert server.os.environ == environment


def test_deleted_file_paths_restore_original_defaults(monkeypatch):
    old = server.PROJECT_ROOT / "old-output"
    monkeypatch.setattr(server, "DOTENV_KEYS", {"OUTPUT_DIR", "CHANNELS_FILE", "LINKS_FILE"})
    server.os.environ["OUTPUT_DIR"] = str(old)
    server.ENV_FILE.write_text(f"OUTPUT_DIR={old}\nCHANNELS_FILE=old-channels.txt\nLINKS_FILE=old-links.txt\n", encoding="utf-8")
    assert server._configured_path("OUTPUT_DIR") == old
    assert client.post("/api/env", json={"raw": "CUSTOM=kept\n"}).status_code == 200
    assert server._configured_path("OUTPUT_DIR") == server.PROJECT_ROOT / "output"
    assert server._configured_path("CHANNELS_FILE") == server.PROJECT_ROOT / "channels.txt"
    assert server._configured_path("LINKS_FILE") == server.PROJECT_ROOT / "links.txt"


def test_prior_output_root_remains_streamable_after_settings_change():
    old = server.PROJECT_ROOT / "old-output"
    old.mkdir()
    output = old / "reel.mp4"
    output.write_bytes(b"test-video" * 300)
    manager = server._job_manager()
    manager.jobs["render_saved"] = {"id": "render_saved", "status": "completed", "created_at": 1,
                                    "output_root": str(old), "output_file": "reel.mp4", "logs": []}
    assert client.post("/api/env", json={"values": {"OUTPUT_DIR": "new-output"}}).status_code == 200
    response = client.get("/api/jobs/render_saved")
    assert "output_root" not in response.json()
    assert client.get(response.json()["output_url"]).content == output.read_bytes()
    assert client.get("/api/outputs/reel.mp4").status_code == 404
    assert client.get("/api/outputs/.env?job_id=render_saved").status_code == 404
    assert client.get("/api/outputs/*.mp4?job_id=render_saved").status_code == 404


def test_settings_atomic_replace_failure_preserves_file_and_environment(monkeypatch):
    from hooky.setup import storage
    server.ENV_FILE.write_text("CUSTOM=original\n", encoding="utf-8")
    environment = server.os.environ.copy()
    def fail_replace(*args):
        raise OSError("Simulated disk failure")
    monkeypatch.setattr(storage.os, "replace", fail_replace)
    response = client.post("/api/env", json={"values": {"CUSTOM": "changed"}})
    assert response.status_code == 500
    assert server.ENV_FILE.read_text(encoding="utf-8") == "CUSTOM=original\n"
    assert server.os.environ == environment
    assert list(server.ENV_FILE.parent.glob("*.tmp")) == []


def test_unchanged_generated_web_token_does_not_request_restart(monkeypatch):
    monkeypatch.setattr(server, "DOTENV_KEYS", set())
    server.os.environ["HOOKY_WEB_TOKEN"] = "generated-token"
    monkeypatch.setattr(server, "_STABLE_SETTINGS", {"STATE_DIR": "", "HOOKY_WEB_TOKEN": "generated-token", "IG_SESSION_FILE": ""})
    response = client.post("/api/env", json={"values": {"CUSTOM": "new"}})
    assert response.status_code == 200
    assert response.json()["restart_required"] == []
    changed = client.post("/api/env", json={"values": {"HOOKY_WEB_TOKEN": "new-token"}})
    assert changed.json()["restart_required"] == ["HOOKY_WEB_TOKEN"]


@pytest.mark.parametrize("key,value", [
    ("AUTO_CLIP_DENSITY_MINUTES", "nan"), ("AUTO_CLIP_DENSITY_MINUTES", "inf"),
    ("AUTO_CLIP_DENSITY_MINUTES", "0"), ("AUTO_CLIP_DENSITY_MINUTES", "-1"),
    ("AUTO_MAX_CLIPS", "broken"), ("AUTO_MAX_CLIPS", "0"),
    ("AUTO_CANDIDATES_PER_CHUNK", "-1"), ("AUTO_CANDIDATES_PER_CHUNK", "1.5"),
    ("PARTS_TARGET_SECONDS", "broken"), ("PARTS_TARGET_SECONDS", "nan"),
    ("PARTS_TARGET_SECONDS", "0.99"), ("PARTS_TARGET_SECONDS", "90.01"),
    ("PARTS_TARGET_SECONDS", ""), ("PARTS_MODE", "invalid"),
    ("PARTS_BACKGROUND", "invalid"), ("PARTS_BACKGROUND", ""),
    ("X264_PRESET", "invalid"), ("X264_PRESET", ""),
    ("SUB_FONT_SIZE", "broken"), ("SUB_FONT_SIZE", "-1"),
    ("SUB_MAX_WORDS", "-1"), ("SUB_MAX_WORDS", "2.5"),
])
@pytest.mark.parametrize("mode", ["raw", "form", "draft"])
def test_known_pipeline_settings_reject_before_any_write_or_job(key, value, mode, monkeypatch):
    server.ENV_FILE.write_bytes(b"# Keep original bytes\r\nCUSTOM_EXTENSION=original\r\n")
    before = server.ENV_FILE.read_bytes()
    launch = Mock(side_effect=AssertionError("Settings validation must not launch a worker"))
    monkeypatch.setattr(server.JobManager, "start", launch)
    values = {key: value}
    if mode == "raw":
        body = {"raw": server.dump_dotenv(values)}
    elif mode == "form":
        body = {"values": values}
    else:
        body = {"draft": [{"values": {"CUSTOM_EXTENSION": "first change"}},
                          {"raw": "CUSTOM_EXTENSION=second change\n"}, {"values": values}]}
    response = client.post("/api/env", json=body)
    assert response.status_code == 400
    assert key in response.json()["detail"]
    assert server.ENV_FILE.read_bytes() == before
    assert not server._JOBS
    launch.assert_not_called()


@pytest.mark.parametrize("mode", ["raw", "form", "draft"])
def test_supported_pipeline_settings_and_extensions_roundtrip(mode):
    values = {
        "AUTO_MAX_CLIPS": "", "AUTO_CANDIDATES_PER_CHUNK": "21", "AUTO_CLIP_DENSITY_MINUTES": "1.5",
        "PARTS_TARGET_SECONDS": "90", "PARTS_MODE": " NUMBER-ONLY ", "PARTS_BACKGROUND": " Plain ",
        "X264_PRESET": " VERYSLOW ", "SUB_FONT_SIZE": "0", "SUB_MAX_WORDS": "",
        "CHANNEL_BACKLOG_MAX": "0", "CHANNEL_SCAN_MAX": "0", "GEMINI_MIN_INTERVAL_SECONDS": "0",
        "SOURCE_MAX_HEIGHT": "", "QUALITY": " maximum ", "SUB_STYLE": " TITANIUM ",
        "GEMINI_MODEL": "custom future gemini model", "WHISPER_MODEL": "Systran/faster-whisper-large-v3",
        "CUSTOM_EXTENSION": "Keep # literal and spaces",
    }
    body = {"raw": server.dump_dotenv(values)} if mode == "raw" else {"values": values}
    if mode == "draft":
        body = {"draft": [{"raw": "CUSTOM_EXTENSION=first\n"}, {"values": values}]}
    response = client.post("/api/env", json=body)
    assert response.status_code == 200
    assert response.json()["values"] == values
    assert server.parse_dotenv(server.ENV_FILE.read_text(encoding="utf-8")) == values


def test_bom_settings_file_and_raw_api_input_work_with_shared_parser():
    server.ENV_FILE.write_bytes("\ufeffSUB_STYLE=gold\r\nCUSTOM_EXTENSION=original\r\n".encode("utf-8"))
    loaded = client.get("/api/env")
    assert loaded.status_code == 200
    assert loaded.json()["values"] == {"SUB_STYLE": "gold", "CUSTOM_EXTENSION": "original"}
    saved = client.post("/api/env", json={"raw": "\ufeffSUB_STYLE=clean\r\nCUSTOM_EXTENSION=changed\r\n"})
    assert saved.status_code == 200
    assert saved.json()["values"] == {"SUB_STYLE": "clean", "CUSTOM_EXTENSION": "changed"}
    assert not server.ENV_FILE.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("endpoint", ["/api/render", "/api/jobs/render_clip", "/api/make", "/api/jobs/make"])
@pytest.mark.parametrize("saved_style", ["", "   "])
def test_omitted_job_style_uses_default_after_saved_blank_normalization(endpoint, saved_style):
    saved = client.post("/api/env", json={"values": {"SUB_STYLE": saved_style}})
    assert saved.status_code == 200
    payload = {"url": "https://example.com/video", "start": 0, "end": 10}
    if endpoint.endswith("make"):
        payload = {"target": payload["url"]}
    started = client.post(endpoint, json=payload)
    assert started.status_code == 200
    job = server._JOBS[started.json()["job_id"]]
    assert job["request"]["style"] == "gold"
