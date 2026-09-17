"""
Generation entry points for the local server: check the request fast, resolve
media ids, start jobs, return. Nothing here waits on a provider.

Every check that can fail is done before a job exists, so a bad request comes
back as a clear message in the same tool call instead of as a failed job the
user has to notice later.
"""

from __future__ import annotations

import httpx

from tools import jobs, video_prep
from tools import media_library as library
from tools.ad_multiplier import KLING, _affordable, _split_list, build_prompt
from tools.image_gen import (
    DEFAULT_MODEL_ID,
    _SINGLE_IMAGE_KEYS,
    _SIZE_KEYS,
    _STYLE_IMAGE_KEY,
    _accepts_image,
    _fetch_models,
    _fetch_schema,
    _find_default_model,
    _first_key,
    _option_values,
    _requires,
    auto_size,
)
from tools.media_library import MediaError
from tools.utils import check_credits_guard

_IMAGE_ROLES = {"image", "input", "input_image", "source", "reference", "image_reference"}
_STYLE_ROLES = {"style", "style_reference", "style_image"}


def _job_lookup(user_id: str):
    return lambda value: jobs.get_job(user_id, value)


def resolve_medias(user_id: str, medias: list[dict] | None) -> tuple[dict | None, dict | None]:
    """(input image entry, style reference entry) from a Higgsfield-style medias list."""
    image = style = None
    for item in medias or []:
        value = str((item or {}).get("value") or "").strip()
        role = str((item or {}).get("role") or "image").strip().lower()
        if not value:
            continue
        entry = library.resolve(user_id, value, job_lookup=_job_lookup(user_id))
        if role in _STYLE_ROLES:
            style = entry
        elif role in _IMAGE_ROLES:
            image = entry
        else:
            raise MediaError(f"Unknown media role '{role}'. Use 'image' for the picture to edit or 'style' for a style reference.")
    return image, style


async def _preflight_image(client, *, model_id: str, prompt: str, has_image: bool,
                           has_style: bool, size: str) -> str:
    """'' when the request is valid for this model, else what to fix."""
    try:
        props = await _fetch_schema(client, model_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"There's no image model called `{model_id}`. Leave `model` empty to use {DEFAULT_MODEL_ID}."
        raise
    if not props:
        return f"Model `{model_id}` has no input schema, so a request can't be built for it."
    name = model_id.replace("-workflow", "")
    if has_image and not _accepts_image(props):
        return f"{name} is text-only and can't edit an image. Leave `model` empty to use {DEFAULT_MODEL_ID}."
    if _requires(props, _first_key(props, _SINGLE_IMAGE_KEYS)) and not has_image:
        return f"{name} edits an existing image — pass one in `medias` with role 'image'."
    if _requires(props, _STYLE_IMAGE_KEY) and not has_style:
        return f"{name} needs a second image as a style reference — pass it in `medias` with role 'style'."
    if "prompt" in props and props["prompt"].get("required") and not prompt:
        return f"{name} needs a prompt describing the result."
    size_key = _first_key(props, _SIZE_KEYS)
    if size and size_key:
        valid = _option_values(props[size_key])
        if valid and size not in valid:
            return f"`{size}` isn't a size {name} supports. Valid: {', '.join(valid)}. Or leave it empty."
    return ""


async def generate_image(user_id: str, *, prompt: str, model: str, medias: list[dict] | None,
                         size: str, optimize_prompt: bool) -> dict:
    image, style = resolve_medias(user_id, medias)
    if image and image["type"] != "image":
        raise MediaError("That media is a video. generate_image edits images; use multiply_ad for video.")

    async with httpx.AsyncClient(timeout=30) as client:
        model_id = model
        if not model_id:
            chosen = _find_default_model(await _fetch_models(client))
            if not chosen:
                raise MediaError("No image models are available right now.")
            model_id = chosen["modelId"]
        problem = await _preflight_image(client, model_id=model_id, prompt=prompt,
                                         has_image=bool(image), has_style=bool(style), size=size)
        if problem:
            raise MediaError(problem)
        guard = await check_credits_guard(client)
        if guard:
            raise MediaError(guard)

    job = jobs.start_image(
        user_id, model_id=model_id, prompt=prompt,
        image_url=image["url"] if image else "", style_image_url=style["url"] if style else "",
        size=size, optimize_prompt=optimize_prompt, caption=prompt,
    )
    return {"jobs": [jobs.public(job)], "model": model_id}


async def multiply_ad(user_id: str, *, source: str, variants: str, references: list[str] | None,
                      model: str, size: str) -> dict:
    if not (source or "").strip():
        raise MediaError(
            "Pass the ad to multiply as `source` (a media_id). For the user's own file call "
            "media_upload_widget first; to reuse an earlier upload call show_medias."
        )
    src = library.resolve(user_id, source, job_lookup=_job_lookup(user_id))

    edits = [e for e in _split_list(variants) if e]
    if not edits:
        raise MediaError("Pass `variants` as one edit per line — each line becomes one output.")

    refs = []
    for i in range(len(edits)):
        value = (references or [])[i] if i < len(references or []) else ""
        refs.append(library.resolve(user_id, value, job_lookup=_job_lookup(user_id)) if value else None)

    async with httpx.AsyncClient(timeout=30) as client:
        guard = await check_credits_guard(client)
        if guard:
            raise MediaError(guard)

        if src["type"] == "image":
            model_id = model or DEFAULT_MODEL_ID
            problem = await _preflight_image(client, model_id=model_id, prompt="x", has_image=True,
                                             has_style=False, size=size)
            if problem:
                raise MediaError(problem)
            warning, _ = await _affordable(client, model_id, len(edits))
            if warning:
                raise MediaError(warning)
            # One size for the whole batch, matched to the source ad's shape.
            if not size:
                props = await _fetch_schema(client, model_id)
                key = _first_key(props, _SIZE_KEYS)
                size = await auto_size(client, props[key], src["url"]) if key else ""
            started = [
                jobs.start_image(
                    user_id, model_id=model_id, prompt=build_prompt(edit, bool(ref), kind="image"),
                    image_url=src["url"], style_image_url=ref["url"] if ref else "",
                    size=size, optimize_prompt=True, label=f"Output {n}", caption=edit,
                )
                for n, (edit, ref) in enumerate(zip(edits, refs), 1)
            ]
            return {"jobs": [jobs.public(j) for j in started], "model": model_id}

        warning, _ = await _affordable(client, KLING["model_id"], len(edits))
        if warning:
            raise MediaError(warning)

    # Probe only (a second or two): an unreadable clip, or one that needs
    # converting on a machine without ffmpeg, is refused here. The conversion
    # itself runs inside the job.
    plan = await video_prep.check(src, KLING)

    started = [
        jobs.start_video(
            user_id, source_media_id=src["media_id"],
            prompt=build_prompt(edit, bool(ref), tagged=True, limit=KLING["prompt_chars"]),
            reference_url=ref["url"] if ref else "",
            label=f"Output {n}", caption=edit,
        )
        for n, (edit, ref) in enumerate(zip(edits, refs), 1)
    ]
    return {"jobs": [jobs.public(j) for j in started], "model": KLING["model_id"],
            "source_changes": plan.changes}
