import httpx
from config import STREETAD_BASE_URL
from tools.utils import bearer_headers, check_credits_guard, download_file, poll_job, QUICK_POLL_TIMEOUT


async def generate_street_ad(
    brand: str = "",
    style: str = "auto",
    surface: str = "brick",
    logo_url: str = "",
    generation_id: str = "",
) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        if not generation_id:
            if not brand:
                return "Please provide a brand name to generate a street ad."

            error = await check_credits_guard(client)
            if error:
                return error

            # Plain (None, value) tuples — no per-part Content-Type header, matching
            # how a browser/curl sends simple multipart fields. Setting an explicit
            # "text/plain" content-type on these (as before) made the Xelta backend
            # treat them like file uploads and route the job through a much slower
            # processing path.
            files = {
                "brand": (None, brand),
                "style": (None, style),
                "surface": (None, surface),
            }

            if logo_url:
                logo_bytes, logo_name = await download_file(client, logo_url)
                files["logo"] = (logo_name, logo_bytes, "image/png")

            r = await client.post(
                f"{STREETAD_BASE_URL}/generate",
                files=files,
                headers=bearer_headers(),
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()

            generation_id = data.get("generation_id")
            if not generation_id:
                return f"Unexpected response: {data}"

        status_url = f"{STREETAD_BASE_URL}/status/{generation_id}"
        try:
            result = await poll_job(client, status_url, timeout=QUICK_POLL_TIMEOUT)
        except TimeoutError:
            return (
                f"Still generating (this can take a few minutes). "
                f"generation_id={generation_id} — call this tool again with that "
                f"generation_id to pick up right where this left off."
            )

        image_url = result.get("output_image_url")
        video_url = result.get("output_video_url")

        lines = ["Street ad ready!"]
        if image_url:
            lines.append(f"Image: {image_url}")
        if video_url:
            lines.append(f"Video: {video_url}")
        return "\n".join(lines) if len(lines) > 1 else f"Done: {result}"
