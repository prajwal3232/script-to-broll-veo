import csv
import io
import json
import os
import queue
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

import azure_storage
import config
import veo
from gemini_client import GeminiClient
from mock_gemini import make_mock
from pipeline import (
    STEP_INPUTS,
    STEP_TITLES,
    run_pipeline,
    run_step,
    sanitize_prompt,
)

os.makedirs(config.GENERATED_DIR, exist_ok=True)

app = Flask(__name__, static_folder="../frontend", static_url_path="")
# Don't let browsers cache the frontend — otherwise a stale app.js/page.js can
# silently break new features (e.g. ignore the "confirm" event and hang forever).
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def _no_cache_frontend(resp):
    """Disable caching for HTML/JS/CSS so UI updates always take effect."""
    ctype = resp.headers.get("Content-Type", "")
    if any(t in ctype for t in ("text/html", "javascript", "text/css")):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


CORS(app)

# In-memory job store. Fine for a single-process dev server.
JOBS: dict[str, dict] = {}

# Persisted run history so previous runs survive a server restart.
JOBS_DIR = os.path.join(config.GENERATED_DIR, "jobs")
HISTORY_PATH = os.path.join(config.GENERATED_DIR, "history.json")
HISTORY_LIMIT = 200
_history_lock = threading.Lock()
os.makedirs(JOBS_DIR, exist_ok=True)


def _script_title(script: str) -> str:
    """A short, human label for a run: its first non-empty line."""
    for line in (script or "").splitlines():
        line = line.strip()
        if line:
            return line[:90]
    return "Untitled run"


def _load_history() -> list[dict]:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _history_upsert(entry: dict) -> None:
    """Insert or update a history entry (newest first), capped at HISTORY_LIMIT."""
    with _history_lock:
        items = [it for it in _load_history() if it.get("id") != entry["id"]]
        items.insert(0, entry)
        items = items[:HISTORY_LIMIT]
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f)
        os.replace(tmp, HISTORY_PATH)


def _history_patch(job_id: str, **fields) -> None:
    """Update specific fields on an existing history entry."""
    with _history_lock:
        items = _load_history()
        for it in items:
            if it.get("id") == job_id:
                it.update(fields)
                break
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f)
        os.replace(tmp, HISTORY_PATH)


def _persist_result(job_id: str, result: dict) -> None:
    """Save a finished run's full result so its prompts/videos page works later."""
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.replace(tmp, path)


def _load_result(job_id: str) -> dict | None:
    try:
        with open(os.path.join(JOBS_DIR, f"{job_id}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _new_job(meta: dict | None = None) -> str:
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "id": job_id,
        "status": "running",
        "events": queue.Queue(),
        "result": None,
        "error": None,
        "confirm_event": threading.Event(),
        "confirm_proceed": False,
        # Separate gate for the "before writing prompts" confirmation (step 5).
        "confirm_prompts_event": threading.Event(),
        "confirm_prompts_proceed": False,
    }
    meta = meta or {}
    _history_upsert(
        {
            "id": job_id,
            "title": meta.get("title", "Untitled run"),
            "status": "running",
            "created_at": time.time(),
            "demo": bool(meta.get("demo")),
            "opts": meta.get("opts", {}),
            "shots": 0,
        }
    )
    return job_id


def _run_job(job_id: str, script: str, demo: bool = False, opts: dict | None = None) -> None:
    job = JOBS[job_id]
    q: queue.Queue = job["events"]
    opts = opts or {}

    def emit(step, status, message, data=None):
        q.put(
            {
                "type": "step",
                "step": step,
                "status": status,
                "message": message,
                "data": data,
            }
        )

    def confirm_prompts(segments) -> bool:
        """Gate before step 5: ask the user to approve the shot breakdown before
        we spend model calls writing Veo prompts. Blocks on its own event."""
        # Persist the shot breakdown so the user can review it while deciding.
        job["result"] = {"script": script, "segments": segments}
        _persist_result(job_id, job["result"])
        _history_patch(job_id, status="awaiting", shots=len(segments))
        q.put({"type": "confirm_prompts", "shots": len(segments), "segments": segments})
        job["status"] = "awaiting"
        ev: threading.Event = job["confirm_prompts_event"]
        proceed = ev.wait(timeout=1800) and job["confirm_prompts_proceed"]
        if proceed:
            job["status"] = "running"
            _history_patch(job_id, status="running")
        return proceed

    try:
        gemini = make_mock(delay=0.7) if demo else None
        result = run_pipeline(script, emit, gemini=gemini, confirm=confirm_prompts)
        segments = result["segments"]
        # Persist prompts now so they're viewable even if the user cancels videos.
        job["result"] = result
        _persist_result(job_id, result)

        # If the user declined at the prompt gate, stop with the shot breakdown.
        if not any(s.get("veo_prompt") for s in segments):
            job["status"] = "done"
            _history_patch(job_id, status="done", shots=len(segments))
            q.put({"type": "complete", "result": result, "cancelled_prompts": True})
            return

        _history_patch(job_id, status="awaiting", shots=len(segments))

        # --- Guardrails #2/#3: cap clips and ask the user before spending. ---
        per_shot = opts.get("number_of_videos", 1)
        max_videos = config.VEO_MAX_VIDEOS_PER_RUN
        max_shots = max(1, max_videos // max(1, per_shot))
        prompted = [s for s in segments if s.get("veo_prompt")]
        render_shots = min(len(prompted), max_shots)
        planned_videos = render_shots * per_shot
        capped = len(prompted) > render_shots

        if planned_videos == 0:
            job["status"] = "done"
            _history_patch(job_id, status="done")
            q.put({"type": "complete", "result": result})
            return

        q.put(
            {
                "type": "confirm",
                "videos": planned_videos,
                "shots": render_shots,
                "per_shot": per_shot,
                "total_shots": len(prompted),
                "capped": capped,
                "max_videos": max_videos,
            }
        )
        job["status"] = "awaiting"

        # Block until the user confirms/cancels (or we time out after 30 min).
        ev: threading.Event = job["confirm_event"]
        if not ev.wait(timeout=1800) or not job["confirm_proceed"]:
            job["status"] = "done"
            _history_patch(job_id, status="done")
            q.put({"type": "complete", "result": result, "cancelled": True})
            return

        job["status"] = "running"
        _history_patch(job_id, status="running")
        _generate_broll(job_id, segments, demo, emit, opts, max_shots)
        job["status"] = "done"
        _persist_result(job_id, result)
        rendered = sum(1 for s in segments if s.get("videos") or s.get("video_demo"))
        _history_patch(job_id, status="done", videos=rendered)
        q.put({"type": "complete", "result": result})
    except Exception as exc:  # noqa: BLE001 — surface any failure to the client
        traceback.print_exc()
        job["status"] = "error"
        job["error"] = str(exc)
        _history_patch(job_id, status="error", error=str(exc))
        q.put({"type": "error", "message": str(exc)})
    finally:
        q.put({"type": "__end__"})


def _generate_broll(
    job_id: str, segments: list, demo: bool, emit, opts: dict, max_shots: int | None = None
) -> None:
    """Step 6: render Veo video(s) per shot and (optionally) upload to Azure.

    Only the first `max_shots` prompted shots are rendered (to honor the
    per-run video cap); any beyond that are marked skipped. Shots are dispatched
    in concurrent waves: up to VEO_BATCH_SIZE are sent for generation at once,
    then we wait VEO_BATCH_DELAY seconds before the next wave. This caps how many
    generations are in flight while keeping throughput high.
    """
    count = opts.get("number_of_videos", 1)
    aspect = opts.get("aspect_ratio", config.VEO_ASPECT_RATIO)
    model = opts.get("model", config.VEO_MODEL)
    batch = max(1, config.VEO_BATCH_SIZE)
    delay = max(0, config.VEO_BATCH_DELAY)
    cap = max_shots if max_shots is not None else len(segments)

    # Pick the shots to render (prompted ones, up to the cap); flag the rest.
    to_render = []
    for i, seg in enumerate(segments, start=1):
        if not seg.get("veo_prompt"):
            continue
        if len(to_render) < cap:
            to_render.append((i, seg))
        else:
            seg["video_skipped"] = True

    total = len(to_render)
    emit(
        6,
        "start",
        f"Generating {total * count} clips across {total} shots "
        f"({model}, {aspect}, {count}×) — up to {batch} at a time",
    )

    done = 0
    for wave_start in range(0, total, batch):
        if wave_start > 0 and delay:
            emit(6, "progress", f"Batch of {batch} sent — waiting {delay}s before the next…")
            time.sleep(delay)
        wave = to_render[wave_start : wave_start + batch]
        with ThreadPoolExecutor(max_workers=batch) as pool:
            futures = [
                pool.submit(
                    _render_one_shot, job_id, i, seg, total, demo, aspect, model, count, emit
                )
                for i, seg in wave
            ]
            for fut in futures:
                fut.result()  # _render_one_shot never raises; this just joins
        done += len(wave)
        emit(6, "progress", f"{done}/{total} shots processed")

    # Every shot has now been attempted at least once. Hand the shots that Veo
    # blocked on content-safety grounds to the prompt-sanitizer for up to 2
    # rewrite-and-retry passes each; non-safety failures are left as-is.
    _retry_safety_blocked(job_id, to_render, total, demo, aspect, model, count, emit)

    emit(6, "done", "B-roll videos ready", {"segments": segments})


def _retry_safety_blocked(
    job_id: str, to_render: list, total: int, demo: bool, aspect: str,
    model: str, count: int, emit,
) -> None:
    """Rewrite-and-retry pass for shots Veo rejected for content safety.

    Runs only after every shot has been attempted once. For each safety-blocked
    shot it asks the prompt-sanitizer to reframe the prompt into policy-safe
    wording, then re-renders — up to 2 attempts. On success the clip is saved and
    the error cleared; if it still fails after 2 tries (or the rewrite itself
    fails, or the failure stops being a safety block) the error stands and we
    move on to the next shot. Non-safety failures are never touched here.
    """
    if demo:
        return
    blocked = [
        (i, seg)
        for (i, seg) in to_render
        if not seg.get("videos")
        and seg.get("video_error")
        and veo.is_safety_block(seg["video_error"])
    ]
    if not blocked:
        return

    try:
        gemini = GeminiClient()
    except Exception as exc:  # noqa: BLE001 — no key/client; can't rewrite
        emit(6, "progress", f"Cannot rewrite blocked prompts: {exc}")
        return

    emit(
        6,
        "progress",
        f"{len(blocked)} shot(s) blocked by the safety filter — "
        f"attempting up to 2 prompt rewrites each…",
    )

    max_attempts = 2
    for i, seg in blocked:
        current = seg.get("veo_prompt", "")
        for attempt in range(1, max_attempts + 1):
            err = seg.get("video_error", "")
            emit(
                6,
                "progress",
                f"Shot {i}/{total}: rewriting prompt to pass safety "
                f"(attempt {attempt}/{max_attempts})…",
            )
            try:
                current = sanitize_prompt(current, err, gemini=gemini)
            except Exception as exc:  # noqa: BLE001 — rewrite failed; give up
                emit(6, "progress", f"Shot {i}/{total}: prompt rewrite failed: {exc}")
                break
            seg["veo_prompt"] = current
            seg.pop("video_error", None)
            _render_one_shot(job_id, i, seg, total, demo, aspect, model, count, emit)
            if seg.get("videos"):
                seg["safety_rewritten"] = True
                emit(
                    6,
                    "progress",
                    f"Shot {i}/{total}: passed safety after rewrite (attempt {attempt}).",
                )
                break
            # Still failing but no longer a safety block — a rewrite won't help.
            if seg.get("video_error") and not veo.is_safety_block(seg["video_error"]):
                break
        if not seg.get("videos") and seg.get("video_error"):
            emit(
                6,
                "progress",
                f"Shot {i}/{total}: still failing after {max_attempts} "
                f"rewrite(s) — {seg['video_error']}",
            )


def _render_one_shot(
    job_id: str, i: int, seg: dict, total: int, demo: bool, aspect: str,
    model: str, count: int, emit,
) -> None:
    """Render (and upload) a single shot. Never raises — failures are recorded
    on the segment so one bad shot doesn't sink the whole wave."""
    sid = seg.get("id", i)
    prompt = seg.get("veo_prompt", "")
    try:
        if demo:
            time.sleep(0.8)
            seg["video_demo"] = True
            seg["video_count"] = count
            seg["azure_path"] = ""
            emit(6, "progress", f"Shot {i}/{total} rendered (demo)")
            return
        if not prompt:
            return
        emit(6, "progress", f"Rendering shot {i}/{total} with Veo…")
        out_path = os.path.join(config.GENERATED_DIR, f"{job_id}_{sid}.mp4")
        paths = veo.generate_video(
            prompt,
            out_path,
            aspect_ratio=aspect,
            model=model,
            number_of_videos=count,
            # The negative prompt is folded into veo_prompt as a labelled block
            # (the Developer API rejects the separate negative_prompt parameter),
            # so we no longer pass it here.
            seed=seg.get("veo_seed"),
            duration_seconds=seg.get("veo_duration") or None,
            on_status=lambda m, i=i: emit(6, "progress", f"Shot {i}/{total}: {m}"),
        )
        videos = []
        azure_paths = []
        for p in paths:
            fname = os.path.basename(p)
            local = f"/api/video/file/{fname}"
            azure = ""
            if azure_storage.is_configured():
                emit(6, "progress", f"Uploading {fname} to Azure…")
                with open(p, "rb") as f:
                    azure = azure_storage.upload_video(f"{job_id}/{fname}", f.read())
            videos.append({"local": local, "azure": azure})
            azure_paths.append(azure)
        seg["videos"] = videos
        seg["video_local"] = videos[0]["local"] if videos else ""
        seg["azure_path"] = azure_paths[0] if azure_paths else ""
        seg["azure_paths"] = azure_paths
        seg.pop("video_error", None)  # clear any error from a prior attempt
    except Exception as exc:  # noqa: BLE001 — keep going on per-shot failure
        traceback.print_exc()
        seg["video_error"] = str(exc)
        emit(6, "progress", f"Shot {i}/{total} failed: {exc}")


@app.post("/api/upload")
def upload():
    body = request.json if request.is_json else {}
    script = ""
    demo = request.args.get("demo") == "1" or bool(body.get("demo"))

    if "file" in request.files:
        script = request.files["file"].read().decode("utf-8", errors="replace")
    else:
        script = body.get("script", "") or request.form.get("script", "")

    script = script.strip()
    if not script and demo:
        from mock_gemini import SAMPLE_SCRIPT

        script = SAMPLE_SCRIPT
    if not script:
        return jsonify({"error": "No script provided"}), 400

    # Guardrail #4 — reject binary files (PDF/docx/images) fed in as "text".
    err = _looks_binary(script)
    if err:
        return jsonify({"error": err}), 400

    # Guardrail #1 — reject oversized documents before they cost tokens.
    if len(script) > config.MAX_SCRIPT_CHARS:
        approx_pages = round(len(script) / 1800)
        return jsonify(
            {
                "error": (
                    f"Script is too long: {len(script):,} characters "
                    f"(~{approx_pages} pages). The limit is "
                    f"{config.MAX_SCRIPT_CHARS:,} characters (~10 pages). "
                    "Trim it or split it into smaller scripts."
                )
            }
        ), 413

    opts = _video_opts(body)

    job_id = _new_job(
        {"title": _script_title(script), "demo": demo, "opts": opts}
    )
    threading.Thread(
        target=_run_job, args=(job_id, script, demo, opts), daemon=True
    ).start()
    return jsonify({"job_id": job_id})


def _looks_binary(text: str) -> str | None:
    """Return an error message if `text` looks like a non-text file, else None.

    A dropped PDF/.docx/image decoded as UTF-8 is full of NUL bytes and U+FFFD
    replacement characters; plain scripts have effectively none.
    """
    if "\x00" in text:
        return (
            "This file doesn't look like plain text (it appears to be a PDF, "
            "Word doc, or other binary file). Export or save it as a .txt file "
            "and upload that, or paste the text directly."
        )
    sample = text[:4000]
    if sample:
        bad = sample.count("�")
        if bad / len(sample) > 0.02:
            return (
                "This file doesn't look like plain text (lots of unreadable "
                "characters). If it's a PDF or Word doc, export it to .txt first, "
                "or paste the script directly."
            )
    return None


def _video_opts(body: dict) -> dict:
    """Validate and clamp the Veo output options from a request body."""
    model = body.get("model")
    if model not in config.VEO_ALLOWED_MODELS:
        model = config.VEO_MODEL
    aspect = body.get("aspect_ratio")
    if aspect not in config.VEO_ASPECT_RATIOS:
        aspect = config.VEO_ASPECT_RATIO
    try:
        count = int(body.get("number_of_videos", 1))
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(config.VEO_MAX_OUTPUTS, count))
    return {"model": model, "aspect_ratio": aspect, "number_of_videos": count}


@app.get("/api/stream/<job_id>")
def stream(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404

    q: queue.Queue = job["events"]

    def gen():
        while True:
            event = q.get()
            if event.get("type") == "__end__":
                break
            yield f"data: {json.dumps(event)}\n\n"

    return Response(gen(), mimetype="text/event-stream")


@app.get("/api/result/<job_id>")
def result(job_id: str):
    job = JOBS.get(job_id)
    if job:
        return jsonify(
            {"status": job["status"], "result": job["result"], "error": job["error"]}
        )
    # Not in memory (e.g. after a restart) — fall back to the persisted result.
    saved = _load_result(job_id)
    if saved is not None:
        return jsonify({"status": "done", "result": saved, "error": None})
    return jsonify({"error": "Unknown job"}), 404


@app.get("/api/jobs")
def jobs_list():
    """List previous runs, newest first, for the history page."""
    return jsonify({"jobs": _load_history()})


@app.post("/api/confirm/<job_id>")
def confirm(job_id: str):
    """Confirm (or cancel) a gated stage.

    `stage` selects which gate to release:
      - "prompts": the gate before step 5 writes the Veo prompts.
      - "videos" (default): the gate before Veo video generation.
    """
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    body = request.get_json(silent=True) or {}
    proceed = bool(body.get("proceed", True))
    stage = body.get("stage", "videos")
    if stage == "prompts":
        job["confirm_prompts_proceed"] = proceed
        job["confirm_prompts_event"].set()
    else:
        job["confirm_proceed"] = proceed
        job["confirm_event"].set()
    return jsonify({"ok": True, "proceed": proceed, "stage": stage})


def _history_get(job_id: str) -> dict | None:
    for it in _load_history():
        if it.get("id") == job_id:
            return it
    return None


@app.post("/api/retry/<job_id>/<shot_id>")
def retry_shot(job_id: str, shot_id: str):
    """Re-generate the video for a single shot (e.g. after a 503 failure).

    Runs in a background thread so the request returns immediately; the videos
    page polls /api/result and re-renders the card when `video_retrying` clears.
    """
    job = JOBS.get(job_id)
    result = (job or {}).get("result") or _load_result(job_id)
    if not result:
        return jsonify({"error": "Unknown job"}), 404
    segments = result.get("segments", [])
    seg = next((s for s in segments if str(s.get("id")) == str(shot_id)), None)
    if seg is None:
        return jsonify({"error": "Unknown shot"}), 404
    if not seg.get("veo_prompt"):
        return jsonify({"error": "This shot has no prompt to generate from."}), 400
    if seg.get("video_retrying"):
        return jsonify({"error": "This shot is already re-generating."}), 409

    rec = _history_get(job_id) or {}
    opts = rec.get("opts") or {}
    demo = bool(rec.get("demo"))
    aspect = opts.get("aspect_ratio") or config.VEO_ASPECT_RATIO
    model = opts.get("model") or config.VEO_MODEL
    try:
        count = int(opts.get("number_of_videos", 1) or 1)
    except (TypeError, ValueError):
        count = 1

    # Clear any previous outcome so the card shows a clean "retrying" state.
    for k in ("video_error", "video_skipped", "videos", "video_local",
              "azure_path", "azure_paths", "video_demo", "video_count"):
        seg.pop(k, None)
    seg["video_retrying"] = True
    _persist_result(job_id, result)  # so disk-backed polls see the retry state

    def _work():
        try:
            _render_one_shot(
                job_id, seg.get("id", shot_id), seg, 1, demo,
                aspect, model, count, lambda *a, **k: None,
            )
        finally:
            seg.pop("video_retrying", None)
            _persist_result(job_id, result)
            if job:
                job["result"] = result

    threading.Thread(target=_work, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/download/<job_id>")
def download(job_id: str):
    job = JOBS.get(job_id)
    result = job["result"] if job and job["result"] else _load_result(job_id)
    if not result:
        return jsonify({"error": "No result for this job"}), 404

    segments = result["segments"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "source_beat",
            "voiceover",
            "visual_extracted",
            "updated_visual",
            "location",
            "characters",
            "veo_prompt",
            "veo_duration_s",
            "azure_broll_path",
        ]
    )
    for s in segments:
        azure = s.get("azure_paths") or [s.get("azure_path", "")]
        chars = s.get("characters") or []
        writer.writerow(
            [
                s.get("id", ""),
                s.get("source_id", ""),
                s.get("voiceover", ""),
                s.get("visual", ""),
                s.get("detailed_visual", ""),
                s.get("location", ""),
                ", ".join(chars) if isinstance(chars, list) else chars,
                s.get("veo_prompt", ""),
                s.get("veo_duration", ""),
                "; ".join(p for p in azure if p),
            ]
        )

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=script_to_broll_{job_id[:8]}.csv"
        },
    )


# --------------------------------------------------------------------------- #
# Step 6 — video generation (Veo 3.1)
# --------------------------------------------------------------------------- #

VIDEO_JOBS: dict[str, dict] = {}


def _run_video_job(job_id: str, prompt: str, demo: bool) -> None:
    job = VIDEO_JOBS[job_id]
    out_path = os.path.join(config.GENERATED_DIR, f"{job_id}.mp4")

    def status(msg: str) -> None:
        job["message"] = msg

    try:
        if demo:
            status("Rendering (demo)…")
            time.sleep(2)
            job["status"] = "demo"
            job["message"] = "Demo mode — connect a Veo-enabled API key to render a real clip."
            return

        veo.generate_video(prompt, out_path, on_status=status)
        job["local_url"] = f"/api/video/file/{job_id}.mp4"

        if azure_storage.is_configured():
            status("Uploading to Azure…")
            with open(out_path, "rb") as f:
                data = f.read()
            job["azure_url"] = azure_storage.upload_video(
                f"{job_id}/{job_id}.mp4", data
            )

        job["status"] = "done"
        job["message"] = "Video ready."
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        job["status"] = "error"
        job["message"] = str(exc)


@app.post("/api/video")
def create_video():
    body = request.get_json(silent=True) or {}
    demo = request.args.get("demo") == "1" or bool(body.get("demo"))
    prompt = (body.get("prompt") or "").strip()
    if not prompt and not demo:
        return jsonify({"error": "No prompt provided"}), 400

    job_id = uuid.uuid4().hex
    VIDEO_JOBS[job_id] = {
        "id": job_id,
        "status": "running",
        "message": "Queued…",
        "local_url": None,
        "azure_url": None,
        "shot_id": body.get("shot_id"),
    }
    threading.Thread(
        target=_run_video_job, args=(job_id, prompt, demo), daemon=True
    ).start()
    return jsonify({"job_id": job_id})


@app.get("/api/video/<job_id>")
def video_status(job_id: str):
    job = VIDEO_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown video job"}), 404
    return jsonify(job)


@app.get("/api/video/file/<path:filename>")
def video_file(filename: str):
    return send_from_directory(config.GENERATED_DIR, filename)


@app.get("/api/steps")
def steps_info():
    """List the individually testable steps and what each one needs."""
    return jsonify(
        {
            str(n): {"title": STEP_TITLES[n], "inputs": STEP_INPUTS[n]}
            for n in sorted(STEP_TITLES)
        }
    )


@app.post("/api/step/<int:n>")
def step(n: int):
    """Run a single pipeline step in isolation.

    Body is a JSON object holding that step's required inputs (see /api/steps).
    Returns only the keys that step produces.
    """
    if n not in STEP_TITLES:
        return jsonify({"error": f"Unknown step {n}. Valid steps are 1-5."}), 404
    payload = request.get_json(silent=True) or {}
    demo = request.args.get("demo") == "1" or bool(payload.pop("demo", False))
    try:
        gemini = make_mock() if demo else None
        return jsonify(run_step(n, payload, gemini=gemini))
    except KeyError as exc:
        return jsonify({"error": exc.args[0] if exc.args else str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


@app.get("/api/health")
def health():
    return jsonify(
        {
            "gemini_key_set": bool(config.GEMINI_API_KEY),
            "gemini_model": config.GEMINI_MODEL,
            "veo_model": config.VEO_MODEL,
            "azure_configured": azure_storage.is_configured(),
            "veo_models": config.VEO_MODELS,
            "veo_aspect_ratios": config.VEO_ASPECT_RATIOS,
            "veo_default_aspect_ratio": config.VEO_ASPECT_RATIO,
            "veo_max_outputs": config.VEO_MAX_OUTPUTS,
        }
    )


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/prompts")
def prompts_page():
    return send_from_directory(app.static_folder, "prompts.html")


@app.get("/videos")
def videos_page():
    return send_from_directory(app.static_folder, "videos.html")


@app.get("/history")
def history_page():
    return send_from_directory(app.static_folder, "history.html")


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=True, threaded=True)
