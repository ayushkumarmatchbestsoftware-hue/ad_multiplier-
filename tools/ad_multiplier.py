"""
Ad Multiplier — turn one source ad clip into N independently edited variants.

One source ad plus an ordered list of edits produces one output per edit, each
rendered separately so a failure isolates to its own position. Ordered lists are
zipped by position, never combined: variant i uses reference image i. N always
counts final outputs, never assets or operations.

This module holds the video model, prompt construction and the provider calls.
Orchestration — resolving media ids, starting one job per variant — lives in
tools/generation.py, and the jobs themselves in tools/jobs.py.

Video edits run on Kling O3 only, which keeps the source audio and takes 3-10s.
"""

import re
import time
from urllib.parse import urlparse

import httpx

from config import IMAGE_GEN_BASE_URL
from tools.credits import check_balance
from tools.creation_recovery import parse_recover_job_id, recover_one
from tools.media_upload import _current_user_id
from tools.utils import bearer_headers

# ── Video model ───────────────────────────────────────────────────────────────
# Video edits always run on Kling O3 — the model verified end to end with real
# user clips. It takes tagged reference images (@Image1 -> imageUrls[0]) and
# keeps the source audio.
#
# "input" is what it accepts as a source clip. tools/video_prep converts any
# upload into it before submitting, because an out-of-range clip fails on the
# provider with nothing more than "execution failed".

KLING = {
    "model_id": "kling-o3-video-edit-workflow",
    "label": "Kling O3 Pro Video Editor",
    # fal's Kling O1/O3 edit contract: MP4/MOV, each side 720-2160px (O3 lists
    # up to 3840; 2160 satisfies both), 24-60 fps, 3-10s, 200 MB.
    "input": {"min_side": 720, "max_side": 2160, "min_fps": 24, "max_fps": 60,
              "min_seconds": 3.0, "max_seconds": 10.0, "max_mb": 200},
    "prompt_chars": 3900,
}

# How long to keep looking for a gateway-cut submission's result before giving
# up. Kling runs 2-5 minutes, so this is well past a slow render.
_RECOVER_GIVE_UP_MS = 25 * 60 * 1000

_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def source_kind(url: str) -> str:
    """'video', 'image', or '' when the extension says neither."""
    path = urlparse(url).path.lower()
    if path.endswith(_VIDEO_EXTS):
        return "video"
    if path.endswith(_IMAGE_EXTS):
        return "image"
    return ""

MAX_PROMPT_CHARS = 2000

# Words that make a person the target of an edit, which adds the clause keeping
# the replaced person out of every frame.
_PERSON_WORDS = (
    "person", "man", "woman", "men", "women", "guy", "girl", "boy", "lady",
    "model", "actor", "actress", "presenter", "host", "performer", "people",
    "face", "hair", "she", "her", "he", "him", "his", "outfit", "clothing",
    "clothes", "wearing", "dress", "shirt", "jacket",
)

# Whole-word match only. Substring matching is wrong here: "he" appears inside
# "the" and "his" inside "this", which sent every background and product edit
# down the person path.
_PERSON_RE = re.compile(r"\b(" + "|".join(_PERSON_WORDS) + r")\b", re.IGNORECASE)

# Carried over verbatim in spirit from the reference implementation: untargeted
# on-screen text must never be silently removed or regenerated.
_PRESERVE_TEXT = (
    "Preserve every caption, subtitle, and other untargeted on-screen text "
    "exactly as it appears, including its wording, styling, placement, "
    "animation and timing."
)

_KEEP_EVERYTHING = (
    "Keep the source clip otherwise exactly as it is: the same shots and cuts, "
    "camera moves, framing, composition, setting, untargeted props, lighting, "
    "pacing and timing."
)

_REFERENCE_AUTHORITY = (
    "The replacement's complete visible appearance comes from the reference "
    "image — face, hair, skin tone, build, clothing, footwear and accessories. "
    "Retain from the source only the original performance, pose, blocking, "
    "screen position and timing."
)

_TRAILER = (
    "Everything else — untouched people, objects, untargeted wardrobe, text, "
    "environment, lighting, camera movement and all timing — stays exactly the same."
)


def build_prompt(
    edit: str,
    has_reference: bool,
    *,
    tagged: bool = False,
    limit: int = MAX_PROMPT_CHARS,
    kind: str = "video",
) -> str:
    """One self-contained edit instruction for a single output position.

    `tagged` cites the reference as @Image1, which Kling resolves against its
    imageUrls array. Image models take the reference untagged, as plain prose.
    """
    edit_text = edit.strip().rstrip(".")
    alias = "@Image1" if tagged else "the reference image"

    reference_authority = (
        f"The replacement's complete visible appearance comes from {alias} — "
        "face, hair, skin tone, build, clothing, footwear and accessories. "
        "Retain from the source only the original performance, pose, blocking, "
        "screen position and timing."
    ) if has_reference else ""

    # Without this, a replaced person tends to survive through cuts, reflections
    # and occlusions — the single biggest fidelity failure in the reference
    # implementation's experience, which is why it mandates the clause.
    exclusion = (
        f"The original person being replaced must not appear in any frame of the "
        f"output — replace them completely with {alias} in every appearance, "
        "including through cuts, entrances, exits, occlusions, motion blur, "
        "reflections and shadows. Every other person stays exactly as they are."
    ) if has_reference and _PERSON_RE.search(edit) else ""

    if kind == "image":
        # A still has no shots, cuts, camera moves or timing to preserve.
        parts = [
            f"Image edit. Change only: {edit_text}.",
            reference_authority.replace("performance, pose, blocking, screen position and timing",
                                        "pose, position and framing"),
            exclusion.replace(" in every appearance, including through cuts, entrances, exits, "
                              "occlusions, motion blur, reflections and shadows", ""),
            "Keep everything else exactly as it is: composition, framing, setting, lighting, "
            "untargeted people and objects.",
            "Preserve all untargeted on-screen text exactly, including wording, styling and placement.",
        ]
    else:
        parts = [
            f"Video edit. Change only: {edit_text}.",
            reference_authority,
            exclusion,
            _KEEP_EVERYTHING,
            _PRESERVE_TEXT,
            _TRAILER,
        ]
    prompt = " ".join(p for p in parts if p)
    if len(prompt) > limit:
        # Drop the trailer first — it is the most redundant sentence — then hard
        # clip, so an over-long edit description still yields a valid request.
        prompt = " ".join(p for p in parts[:-1] if p)
        prompt = prompt[:limit].rstrip()
    return prompt


def _split_list(raw: str) -> list[str]:
    """Split a newline- or semicolon-separated list, preserving empty slots."""
    if not raw:
        return []
    separator = "\n" if "\n" in raw else ";"
    return [item.strip() for item in raw.split(separator)]


def _result_url(payload: dict) -> str:
    """Dig a hosted video URL out of whichever shape the API returns."""
    candidates = [payload, payload.get("data") or {}, (payload.get("data") or {}).get("result") or {}]
    for scope in candidates:
        if not isinstance(scope, dict):
            continue
        for key in ("videoUrl", "video_url", "url", "outputUrl", "resultUrl", "output", "content"):
            value = scope.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
    return ""


def _job_id(payload: dict) -> str:
    for scope in (payload, payload.get("data") or {}):
        if not isinstance(scope, dict):
            continue
        for key in ("id", "jobId", "job_id", "generationId", "generation_id", "taskId", "task_id"):
            value = scope.get(key)
            if value:
                return str(value)
    return ""


async def _submit(client: httpx.AsyncClient, *, video_url: str, prompt: str, reference_url: str) -> dict:
    payload = {"videoUrl": video_url, "prompt": prompt, "keepAudio": True}
    if reference_url:
        payload["imageUrls"] = [reference_url]   # @Image1 in the prompt

    r = await client.post(
        f"{IMAGE_GEN_BASE_URL}/api/models/{KLING['model_id']}/generate",
        headers={**bearer_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


async def _poll_once(client: httpx.AsyncClient, job_id: str, model_id: str) -> tuple[str, str, str]:
    """Return (state, video_url, error). state is 'done', 'failed' or 'pending'.

    Only genuinely transient answers (5xx, 429, network) stay 'pending'. Anything
    that will never resolve by waiting fails straight away, because the viewer
    polls pending items in the background and would otherwise poll forever.
    """
    since_ms = parse_recover_job_id(job_id)
    if since_ms is not None:
        # Submitted, but the gateway cut the request before a job id came back.
        # The result lands in the creations folder when it finishes.
        url = await recover_one(_current_user_id(), model_id, since_ms)
        if url:
            return "done", url, ""
        if time.time() * 1000 - since_ms > _RECOVER_GIVE_UP_MS:
            return "failed", "", "Xelta accepted this but the result never arrived. Try again."
        return "pending", "", ""

    try:
        r = await client.get(
            f"{IMAGE_GEN_BASE_URL}/api/models/{model_id}/status/{job_id}",
            headers=bearer_headers(),
            timeout=20,
        )
    except httpx.HTTPError:
        return "pending", "", ""

    # The status route answers an unknown job id with 401 "Session expired or
    # invalidated" rather than 404, even while the same token generates fine —
    # so a 401 here can't be read as a dead sign-in. Stop polling either way.
    if r.status_code in (400, 401, 403, 404):
        return "failed", "", "Xelta couldn't find this job. Try creating it again."
    if r.status_code != 200:
        return "pending", "", ""

    data = r.json()
    scope = data.get("data") if isinstance(data.get("data"), dict) else data
    status = str(scope.get("status") or scope.get("state") or "").lower()
    url = _result_url(data)
    if url:
        return "done", url, ""
    if status in ("failed", "error", "cancelled"):
        reason = scope.get("error") or scope.get("message") or "generation failed"
        return "failed", "", str(reason)[:160]
    return "pending", "", ""


async def _model_cost(client: httpx.AsyncClient, model_id: str) -> int:
    """Credit cost for one generation, read from the model's own record.

    Uses the per-model endpoint rather than a category listing: the video engine
    lives under `video_editing`, so looking it up in the image catalogue silently
    returned 0 and disabled the guard on the expensive path.
    """
    r = await client.get(
        f"{IMAGE_GEN_BASE_URL}/api/models/{model_id}",
        headers=bearer_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return int((r.json().get("data") or {}).get("creditCost") or 0)


async def _affordable(client: httpx.AsyncClient, model_id: str, count: int) -> tuple[str, int]:
    """(error_message, unit_cost). Refuses before spending anything partial."""
    try:
        unit = await _model_cost(client, model_id)
        balance = int((await check_balance()).get("data") or 0)
    except Exception:
        return "", 0  # never block on a failed preflight; the service still enforces

    if not unit:
        return "", 0
    total = unit * count
    if total > balance:
        affordable = balance // unit
        return (
            f"That needs {total} credits ({count} x {unit}) but you have {balance}.\n"
            f"You can afford {affordable} variant{'s' if affordable != 1 else ''} right now — "
            f"rerun with {affordable} line{'s' if affordable != 1 else ''} in `variants`, "
            f"or top up at https://www.xelta.ai."
        ), unit
    return "", unit
