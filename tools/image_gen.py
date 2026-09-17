"""
Image generation — text-to-image and image-to-image.

Xelta exposes ~21 image models, and each one declares its own input schema at
GET /api/models/<model_id>. Those schemas differ a lot: the input image is
called `image` on SeeDream and Gemini, `input_image` on FLUX Kontext,
`initImage` on style transfer, `image_prompt` on FLUX dev, and `imageUrls` (an
array) on Grok. Output dimensions are `size` on six models and `aspectRatio`,
`aspect_ratio`, `resolution` or `width`/`height` on the rest.

So the request is built from the chosen model's own schema rather than from one
fixed shape: only keys the model declares are sent, a value the user picks is
checked against that model's own option list, and any required key the caller
never sees (`model`, `quality`, flux-dev's `width`/`height`) is filled from the
default the schema declares.
"""

import asyncio
import time

import httpx
from config import IMAGE_GEN_BASE_URL
from tools.creation_recovery import GATEWAY_TIMEOUT_STATUSES, recover_one
from tools.utils import bearer_headers, check_credits_guard

# Gemini 2.5 Flash ("Nano Banana"). Its `image` input is optional, so one model
# serves both text-to-image and image-to-image, and it is the cheapest of the
# editing-capable models at 59 credits.
DEFAULT_MODEL_ID = "gemini-2-5-flash-image-workflow"
DEFAULT_MODEL_FALLBACK_MATCH = "flux"

# Keys a model may expect a single input image under, in priority order.
_SINGLE_IMAGE_KEYS = ("image", "input_image", "initImage", "image_prompt")
# Array equivalents, for models that only take a list (Grok, SeeDream multi-ref).
_ARRAY_IMAGE_KEYS = ("images", "imageUrls")
# Only stable-image-style-transfer takes a second, separate style reference.
_STYLE_IMAGE_KEY = "styleImage"
# Whichever of these a model declares is how it takes its output dimensions.
_SIZE_KEYS = ("size", "aspectRatio", "aspect_ratio", "resolution")

# Never auto-filled from a schema default: the caller supplies these, and the
# declared defaults are demo placeholders (Gemini's default prompt is its own
# thumbnail copy) that would quietly generate the wrong thing.
_NEVER_AUTOFILL = {"prompt", *_SINGLE_IMAGE_KEYS, *_ARRAY_IMAGE_KEYS, _STYLE_IMAGE_KEY}

# Model schemas are static per deploy — fetch each at most once per process.
_schema_cache: dict[str, dict] = {}


# ── model + schema lookup ─────────────────────────────────────────────────────

async def _fetch_models(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"{IMAGE_GEN_BASE_URL}/api/models/image_generation/all",
        headers=bearer_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", [])


async def _fetch_schema(client: httpx.AsyncClient, model_id: str) -> dict:
    """Return the model's inputSchema properties, or {} if it declares none."""
    if model_id in _schema_cache:
        return _schema_cache[model_id]
    r = await client.get(
        f"{IMAGE_GEN_BASE_URL}/api/models/{model_id}",
        headers=bearer_headers(),
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data") or {}
    props = (data.get("inputSchema") or {}).get("properties") or {}
    _schema_cache[model_id] = props
    return props


def _find_default_model(models: list[dict]) -> dict | None:
    for m in models:
        if m.get("modelId") == DEFAULT_MODEL_ID:
            return m
    # Gemini pulled from the catalogue — fall back to any FLUX model.
    for m in models:
        if DEFAULT_MODEL_FALLBACK_MATCH in m.get("modelId", "").lower():
            return m
    return models[0] if models else None


def _format_models_prompt(models: list[dict]) -> str:
    if not models:
        return "No image generation models found."
    lines = [f"**Available image generation models** ({len(models)} total)\n"]
    for i, m in enumerate(models, 1):
        lines.append(
            f"{i}. **{m['displayName'].strip()}**\n"
            f"   ID: `{m['modelId']}`\n"
            f"   {m.get('description', '')} | {(m.get('transformationType') or '').strip()} | "
            f"{m.get('creditCost', '?')} credits | ~{m.get('estimatedTime', '?')}"
        )
    lines.append("\nWhich model would you like to use?")
    return "\n".join(lines)


# ── schema helpers ────────────────────────────────────────────────────────────

def _option_values(prop: dict) -> list[str]:
    return [str(o.get("value")) for o in (prop.get("options") or []) if o.get("value") is not None]


def _first_key(props: dict, candidates: tuple[str, ...]) -> str | None:
    for key in candidates:
        if key in props:
            return key
    return None


def _accepts_image(props: dict) -> bool:
    return bool(_first_key(props, _SINGLE_IMAGE_KEYS) or _first_key(props, _ARRAY_IMAGE_KEYS))


def _requires(props: dict, key: str | None) -> bool:
    return bool(key and props.get(key, {}).get("required"))


def _size_options_text(prop: dict, model_name: str) -> str:
    label = (prop.get("name") or "size").lower()
    article = "an" if label[:1] in "aeiou" else "a"
    lines = [f"**{model_name}** — pick {article} {label}:\n"]
    options = prop.get("options") or []
    for i, opt in enumerate(options, 1):
        marker = "  *(default)*" if str(opt.get("value")) == str(prop.get("defaultValue")) else ""
        lines.append(f"{i}. `{opt.get('value')}` — {opt.get('label', opt.get('value'))}{marker}")
    if not options:
        lines.append(f"Provide a value (default: `{prop.get('defaultValue')}`).")
    lines.append("\nWhich would you like? Pass it as `size`.")
    return "\n".join(lines)


def _build_payload(
    props: dict,
    *,
    prompt: str,
    image_url: str,
    style_image_url: str,
    size: str,
    optimize_prompt: bool,
) -> dict:
    payload: dict = {}

    if "prompt" in props and prompt:
        payload["prompt"] = prompt

    if image_url:
        key = _first_key(props, _SINGLE_IMAGE_KEYS)
        if key:
            payload[key] = image_url
        else:
            array_key = _first_key(props, _ARRAY_IMAGE_KEYS)
            if array_key:
                payload[array_key] = [image_url]

    if style_image_url and _STYLE_IMAGE_KEY in props:
        payload[_STYLE_IMAGE_KEY] = style_image_url

    size_key = _first_key(props, _SIZE_KEYS)
    if size_key and size:
        payload[size_key] = size

    # Only two SeeDream models expose prompt optimisation, and "standard" is the
    # single accepted value — there is no documented "off", so the key is simply
    # omitted when the caller opts out.
    if "optimize_prompt_mode" in props and optimize_prompt:
        payload["optimize_prompt_mode"] = "standard"

    # Fill remaining required keys the caller never sees from their declared
    # defaults (e.g. `model`, `quality`, flux-dev's `width`/`height`).
    for key, prop in props.items():
        if key in payload or key in _NEVER_AUTOFILL or not prop.get("required"):
            continue
        default = prop.get("defaultValue")
        if default is None:
            options = _option_values(prop)
            default = options[0] if options else None
        if default is not None:
            payload[key] = default

    return payload


# ── automatic size ────────────────────────────────────────────────────────────

def image_dimensions(head: bytes) -> tuple[int, int] | None:
    """(width, height) from the first bytes of a PNG, JPEG or WebP, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")

    if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and len(head) >= 30:
        chunk = head[12:16]
        if chunk == b"VP8X":
            return (int.from_bytes(head[24:27], "little") + 1,
                    int.from_bytes(head[27:30], "little") + 1)
        if chunk == b"VP8 ":
            return (int.from_bytes(head[26:28], "little") & 0x3FFF,
                    int.from_bytes(head[28:30], "little") & 0x3FFF)
        if chunk == b"VP8L" and len(head) >= 25:
            bits = int.from_bytes(head[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1

    if head.startswith(b"\xff\xd8"):
        # Walk JPEG segments to the first start-of-frame marker. EXIF blocks can
        # push it well past the first few KB, hence the larger read in auto_size.
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = int.from_bytes(head[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return int.from_bytes(head[i + 7:i + 9], "big"), int.from_bytes(head[i + 5:i + 7], "big")
            i += 2 + length
    return None


def _ratio(value: str) -> float | None:
    """'16:9' / '1328*1328' / '2048x2048' -> width/height; None for '2K', 'auto'."""
    for sep in (":", "*", "x"):
        if sep in value:
            left, _, right = value.partition(sep)
            try:
                w, h = float(left), float(right)
                return w / h if w > 0 and h > 0 else None
            except ValueError:
                return None
    return None


def closest_option(options: list[str], width: int, height: int) -> str:
    """The option whose aspect ratio is nearest the source's, or '' if none parse."""
    import math
    target = math.log(width / height)
    scored = [(abs(math.log(r) - target), opt) for opt in options if (r := _ratio(opt))]
    return min(scored)[1] if scored else ""


async def auto_size(client: httpx.AsyncClient, prop: dict, image_url: str) -> str:
    """Choose a size so the user isn't asked: match the source photo's shape when
    editing one, otherwise fall back to the model's declared default."""
    default = str(prop.get("defaultValue") or "")
    options = _option_values(prop)
    if image_url and options:
        try:
            r = await client.get(image_url, headers={"Range": "bytes=0-262143"}, timeout=15)
            if r.status_code in (200, 206):
                dims = image_dimensions(r.content)
                if dims and all(dims):
                    return closest_option(options, *dims) or default
        except Exception:
            pass
    return default or (options[0] if options else "")


async def _await_creation(model_id: str, sent_ms: int, attempts: int = 8, interval: float = 5) -> str:
    """Poll storage briefly for a generation whose request the gateway cut off."""
    from tools.media_upload import _current_user_id

    user_id = _current_user_id()
    for _ in range(attempts):
        url = await recover_one(user_id, model_id, sent_ms)
        if url:
            return url
        await asyncio.sleep(interval)
    return ""


# ── tool ──────────────────────────────────────────────────────────────────────

async def generate_image(
    prompt: str = "",
    model_id: str = "",
    size: str = "",
    optimize_prompt: bool = True,
    image_url: str = "",
    style_image_url: str = "",
) -> str:
    async with httpx.AsyncClient() as client:
        if not model_id:
            models = await _fetch_models(client)
            chosen = _find_default_model(models)
            if not chosen:
                return _format_models_prompt(models)
            model_id = chosen["modelId"]

        try:
            props = await _fetch_schema(client, model_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                models = await _fetch_models(client)
                return f"No model with ID `{model_id}`.\n\n" + _format_models_prompt(models)
            raise

        if not props:
            return f"Model `{model_id}` returned no input schema, so its request can't be built."

        model_name = model_id.replace("-workflow", "")
        image_key = _first_key(props, _SINGLE_IMAGE_KEYS)

        # An image was supplied to a model that can't take one.
        if image_url and not _accepts_image(props):
            return (
                f"**{model_name}** is text-only — it doesn't accept an input image.\n\n"
                "For image-to-image, call again with `model_id` left empty (defaults to "
                f"`{DEFAULT_MODEL_ID}`, 59 credits) or name an editing model such as "
                "`flux-kontext-pro-workflow`."
            )

        # The model needs an image and none was given.
        if _requires(props, image_key) and not image_url:
            return (
                f"**{model_name}** is image-to-image — it needs an input image.\n\n"
                "Call `upload_media` so the user can upload one, then pass the "
                "`https://media.xelta.ai/...` URL it posts back as `image_url`."
            )
        if _requires(props, _STYLE_IMAGE_KEY) and not style_image_url:
            return (
                f"**{model_name}** needs two images: the image to restyle (`image_url`) "
                "and a style reference (`style_image_url`).\n\n"
                "Call `upload_media` once for each and pass both URLs."
            )
        if "prompt" in props and props["prompt"].get("required") and not prompt:
            return f"**{model_name}** needs a prompt describing the image to generate."

        # Size — driven by whichever key this model actually declares. When the
        # caller didn't choose, pick one instead of asking: the closest ratio to
        # the source photo for an edit, else the model's own default.
        size_key = _first_key(props, _SIZE_KEYS)
        if size_key:
            valid = _option_values(props[size_key])
            if not size:
                size = await auto_size(client, props[size_key], image_url)
            if valid and size not in valid:
                return (
                    f"`{size}` isn't a valid option for **{model_name}**.\n\n"
                    + _size_options_text(props[size_key], model_name)
                )

        guard = await check_credits_guard(client)
        if guard:
            return guard

        payload = _build_payload(
            props,
            prompt=prompt,
            image_url=image_url,
            style_image_url=style_image_url,
            size=size,
            optimize_prompt=optimize_prompt,
        )

        sent_ms = int(time.time() * 1000)
        r = await client.post(
            f"{IMAGE_GEN_BASE_URL}/api/models/{model_id}/generate",
            headers={**bearer_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        if r.status_code in GATEWAY_TIMEOUT_STATUSES:
            # The gateway cuts requests at 29s but the generation carries on and
            # is billed, so wait for it to land in storage instead of failing.
            recovered = await _await_creation(model_id, sent_ms)
            if recovered:
                return (
                    f"Image {'edited' if image_url else 'generated'} with **{model_id}**\n"
                    f"URL: {recovered}"
                )
            r.raise_for_status()
        r.raise_for_status()
        resp = r.json()

        result = resp.get("data", {}).get("result", {})
        content = result.get("content", "")

        # Prefer an actual hosted URL. Some models return inline base64/data-URI
        # image data in `content` instead — that's not a usable link and renders
        # as a broken/placeholder icon on the client, so surface a clear message
        # rather than passing it through as if it were a URL.
        out_url = ""
        for key in ("url", "imageUrl", "publicUrl", "outputUrl", "resultUrl", "content"):
            val = result.get(key, "")
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                out_url = val
                break

        if not out_url:
            if not content:
                return f"Generation failed: {resp}"
            return (
                "Image generated, but the API returned inline image data instead of "
                "a hosted URL, which can't be displayed as a link. Try a different "
                "model, or check the Xelta dashboard for the result."
            )

        meta = resp.get("metadata", {})
        res_meta = result.get("metadata", {})
        new_balance = resp.get("creditTransaction", {}).get("newBalance")

        lines = [
            ("Image edited" if image_url else "Image generated")
            + f" with **{meta.get('modelName', model_id)}**"
        ]
        if image_url:
            lines.append(f"Source image: {image_url}")
        lines.append(
            f"Resolution: {res_meta.get('size', size or '?')} | "
            f"Time: {meta.get('executionTime', '?')}"
        )
        lines.append(f"URL: {out_url}")
        if new_balance is not None:
            lines.append(f"Remaining balance: {new_balance} credits")
        return "\n".join(lines)
