"""
Generation jobs — every generation returns a job_id immediately.

Modelled on Higgsfield: generate_* never holds the tool call open while the
provider works. It records a job, starts the work in the background, and
returns. The generation widget then polls job_status (which answers instantly
with a poll_after_seconds hint) and swaps the result in when it lands. Long
blocking calls were what made the chat look stuck.

Jobs are persisted so job_status still answers after a server restart. A job
whose worker is gone is resolved from R2, where Xelta stores every result
(see creation_recovery); the same path recovers submissions the API gateway
cut off at 29 seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

from tools import media_library as library
from tools.media_library import MediaError
from tools.creation_recovery import GATEWAY_TIMEOUT_STATUSES, recover_job_id, recover_one

JOBS_FILE = Path(
    os.getenv("XELTA_JOBS_FILE", "")
    or Path(__file__).resolve().parent.parent / ".xelta_jobs.json"
)
MAX_JOBS = 300
POLL_AFTER = {"image": 3, "video": 15}
GIVE_UP_MS = 25 * 60 * 1000
TERMINAL = ("completed", "failed")

_lock = threading.Lock()
_running: dict[str, asyncio.Task] = {}


# ── storage ───────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"jobs": {}, "order": []}


def _save(data: dict) -> None:
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, JOBS_FILE)


def _write(job: dict) -> dict:
    with _lock:
        data = _load()
        data["jobs"][job["job_id"]] = job
        data["order"] = [job["job_id"]] + [j for j in data["order"] if j != job["job_id"]]
        for dropped in data["order"][MAX_JOBS:]:
            data["jobs"].pop(dropped, None)
        data["order"] = data["order"][:MAX_JOBS]
        _save(data)
    return job


def _update(job_id: str, **fields) -> dict | None:
    with _lock:
        data = _load()
        job = data["jobs"].get(job_id)
        if not job:
            return None
        job.update(fields)
        _save(data)
        return dict(job)


def get_job(user_id: str, job_id: str) -> dict | None:
    job = _load()["jobs"].get((job_id or "").strip())
    return dict(job) if job and job.get("user_id") == user_id else None


def public(job: dict) -> dict:
    """The job as tools and the widget see it — never the provider internals."""
    return {
        "job_id": job["job_id"],
        "type": job["type"],
        "status": job["status"],
        "model": job["model"],
        "label": job.get("label", ""),
        "caption": job.get("caption", ""),
        # "preparing" while a source clip is converted for the model, with
        # stage_detail saying what changed; "rendering" once it's submitted.
        "stage": job.get("stage", ""),
        "stage_detail": job.get("stage_detail", ""),
        # What the card shows about the request: model, aspect, seconds, audio, credits.
        "meta": job.get("meta", {}),
        "can_recreate": bool(job.get("inputs")),
        "results": job.get("results", []),
        "error": job.get("error", ""),
        "recovery_tool": job.get("recovery_tool", ""),
        "poll_after_seconds": None if job["status"] in TERMINAL else POLL_AFTER.get(job["type"], 5),
    }


def _new(user_id: str, kind: str, model: str, *, engine: str = "", label: str = "", caption: str = "",
         inputs: dict | None = None, meta: dict | None = None) -> dict:
    return _write({
        "inputs": inputs or {},
        "meta": meta or {},
        "job_id": str(uuid.uuid4()),
        "user_id": user_id,
        "type": kind,
        "model": model,
        "engine": engine,
        "status": "queued",
        "created_ms": int(time.time() * 1000),
        "sent_ms": 0,
        "provider_job_id": "",
        "results": [],
        "error": "",
        "recovery_tool": "",
        "label": label,
        "caption": caption[:200],
    })


def _spawn(job_id: str, coro) -> None:
    task = asyncio.create_task(coro)
    _running[job_id] = task
    task.add_done_callback(lambda _t: _running.pop(job_id, None))


def _complete(job: dict, url: str, kind: str) -> dict:
    media = library.add(job["user_id"], url, kind, "generated", caption=job.get("caption", ""))
    return _update(job["job_id"], status="completed", error="",
                   results=[{"media_id": media["media_id"], "url": media["url"], "type": kind}]) or job


def _provider_message(response: httpx.Response) -> str:
    """Xelta's own explanation from an error response, or ''."""
    try:
        body = response.json()
    except Exception:
        return (response.text or "").strip()[:240]
    if isinstance(body, dict):
        for key in ("message", "error", "detail", "reason"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("detail")
            if isinstance(value, str) and value.strip():
                return value.strip()[:240]
    return str(body)[:240]


def _fail_from_http(job_id: str, e: httpx.HTTPStatusError) -> None:
    code = e.response.status_code
    reason = _provider_message(e.response)
    # Keep Xelta's own reason: a bare "error (500), try again" made a request
    # that will never succeed look like a passing glitch.
    print(f"[jobs] {job_id} provider HTTP {code}: {reason}", file=sys.stderr, flush=True)
    if code in (401, 403):
        _update(job_id, status="failed", recovery_tool="xelta_login",
                error="Your Xelta sign-in has expired or was revoked.")
    else:
        _update(job_id, status="failed",
                error=f"Xelta couldn't process this ({code})" + (f": {reason}" if reason else "."))


# ── image ─────────────────────────────────────────────────────────────────────

def start_image(user_id: str, *, model_id: str, prompt: str, image_url: str, style_image_url: str,
                size: str, optimize_prompt: bool, label: str = "", caption: str = "",
                meta: dict | None = None) -> dict:
    inputs = {"model_id": model_id, "prompt": prompt, "image_url": image_url,
              "style_image_url": style_image_url, "size": size, "optimize_prompt": optimize_prompt}
    job = _new(user_id, "image", model_id, label=label, caption=caption or prompt, inputs=inputs, meta=meta)
    _spawn(job["job_id"], _run_image(job["job_id"], model_id, prompt, image_url,
                                     style_image_url, size, optimize_prompt))
    return job


async def _run_image(job_id, model_id, prompt, image_url, style_image_url, size, optimize_prompt):
    from tools.image_gen import generate_image
    from tools.media_view import labeled_url

    job = _update(job_id, status="in_progress", sent_ms=int(time.time() * 1000))
    try:
        # generate_image already recovers a gateway-cut request from R2.
        out = await generate_image(prompt, model_id, size, optimize_prompt, image_url, style_image_url)
    except httpx.HTTPStatusError as e:
        _fail_from_http(job_id, e)
        return
    except Exception as e:
        print(f"[jobs] image job {job_id} crashed: {e}", file=sys.stderr, flush=True)
        _update(job_id, status="failed", error="Something went wrong creating this image.")
        return
    url = labeled_url(out)
    if url:
        _complete(job, url, "image")
    else:
        _update(job_id, status="failed", error=out.strip()[:300])


# ── video ─────────────────────────────────────────────────────────────────────

def start_video(user_id: str, *, source_media_id: str, prompt: str, reference_url: str,
                label: str = "", caption: str = "", seconds: float = 0, meta: dict | None = None) -> dict:
    from tools.ad_multiplier import KLING

    inputs = {"source_media_id": source_media_id, "prompt": prompt,
              "reference_url": reference_url, "seconds": seconds}
    job = _new(user_id, "video", KLING["model_id"], label=label, caption=caption, inputs=inputs, meta=meta)
    _spawn(job["job_id"], _run_video(job["job_id"], source_media_id, prompt, reference_url, seconds))
    return job


def recreate(user_id: str, job_id: str) -> dict:
    """Start a new job with exactly the inputs of an earlier one (charged again)."""
    job = get_job(user_id, job_id)
    if not job:
        raise MediaError(f"No job with id {job_id}.")
    inputs = job.get("inputs")
    if not inputs:
        raise MediaError("This one was made before Recreate was available. Ask Claude to run the same edit again.")
    same = {"label": job.get("label", ""), "caption": job.get("caption", ""), "meta": job.get("meta") or {}}
    if job["type"] == "video":
        return start_video(user_id, **inputs, **same)
    return start_image(user_id, **inputs, **same)


async def _run_video(job_id, source_media_id, prompt, reference_url, seconds=0):
    from tools import video_prep
    from tools.ad_multiplier import KLING, _job_id, _result_url, _submit

    job = _update(job_id, status="in_progress", stage="preparing", stage_detail="")
    try:
        # Kling gets the clip in a form it accepts; a clip that already fits
        # comes back untouched, without a download.
        clip = await video_prep.prepare(
            job["user_id"], source_media_id, KLING,
            on_convert=lambda plan: _update(job_id, stage_detail=plan.summary()),
            seconds=seconds,
        )
    except MediaError as e:
        _update(job_id, status="failed", stage="", error=str(e))
        return
    except Exception as e:
        print(f"[jobs] video job {job_id} prep crashed: {e!r}", file=sys.stderr, flush=True)
        _update(job_id, status="failed", stage="", error="Something went wrong preparing the video.")
        return

    sent_ms = int(time.time() * 1000)
    # The card's chips describe the clip Kling actually gets.
    meta = {**(job.get("meta") or {}), "aspect": video_prep.aspect_label(clip.width, clip.height),
            "seconds": round(clip.seconds), "audio": clip.audio}
    job = _update(job_id, sent_ms=sent_ms, stage="rendering", source_sent=clip.url,
                  prep_changes=clip.changes, meta=meta) or job
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await _submit(client, video_url=clip.url, prompt=prompt, reference_url=reference_url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in GATEWAY_TIMEOUT_STATUSES:
            # Not a failure: the job runs on behind the gateway and is billed.
            _update(job_id, provider_job_id=recover_job_id(sent_ms))
            return
        _fail_from_http(job_id, e)
        return
    except Exception as e:
        print(f"[jobs] video job {job_id} crashed: {e}", file=sys.stderr, flush=True)
        _update(job_id, status="failed", error="Something went wrong submitting this video.")
        return

    url, provider_id = _result_url(response), _job_id(response)
    if url:
        _complete(job, url, "video")
    elif provider_id:
        _update(job_id, provider_job_id=provider_id)
    else:
        _update(job_id, status="failed", error="Xelta accepted the request but returned no job to track.")


# ── status ────────────────────────────────────────────────────────────────────

async def _refresh(job: dict) -> dict:
    if job["status"] in TERMINAL:
        return job
    now_ms = int(time.time() * 1000)

    if job["type"] == "video" and job.get("provider_job_id"):
        from tools.ad_multiplier import _poll_once
        async with httpx.AsyncClient(timeout=30) as client:
            state, url, error = await _poll_once(client, job["provider_job_id"], job["model"])
        if state == "done":
            return _complete(job, url, "video")
        if state == "failed":
            return _update(job["job_id"], status="failed", error=error) or job
        return job

    # Worker gone (server restarted, or another process started it): the result,
    # if there is one, is already sitting in R2.
    if job["status"] == "in_progress" and job["job_id"] not in _running and job.get("sent_ms"):
        url = await recover_one(job["user_id"], job["model"], job["sent_ms"])
        if url:
            return _complete(job, url, job["type"])
        if now_ms - job["sent_ms"] > GIVE_UP_MS:
            return _update(job["job_id"], status="failed",
                           error="This didn't come back from Xelta. Try again.") or job
    # Worker gone before anything was submitted (e.g. a restart mid-conversion):
    # nothing will ever arrive, and nothing was charged.
    if (job["status"] in ("queued", "in_progress") and job["job_id"] not in _running
            and not job.get("sent_ms") and now_ms - job["created_ms"] > GIVE_UP_MS):
        return _update(job["job_id"], status="failed", stage="",
                       error="Preparing this was interrupted. Try again.") or job
    return job


async def job_status(user_id: str, job_id: str, *, wait_seconds: int = 0) -> dict | None:
    job = get_job(user_id, job_id)
    if not job:
        return None
    deadline = time.time() + max(0, min(wait_seconds, 15))
    while True:
        job = await _refresh(job)
        if job["status"] in TERMINAL or time.time() >= deadline:
            return public(job)
        await asyncio.sleep(min(3, max(0.5, deadline - time.time())))
        job = get_job(user_id, job_id) or job
