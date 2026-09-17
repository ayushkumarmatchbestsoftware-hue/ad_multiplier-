import asyncio
import httpx
from config import REEL_BASE_URL
from tools.utils import bearer_headers, check_credits_guard, load_file, QUICK_POLL_ITERATIONS


async def create_reel(prompt: str = "", image_url: str = "", reel_id: str = "") -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        if not reel_id:
            if not prompt:
                return "Please provide a prompt to generate a reel."

            error = await check_credits_guard(client)
            if error:
                return error

            # Step 1: Submit prompt
            r = await client.post(
                f"{REEL_BASE_URL}/api/reels",
                json={"prompt": prompt},
                headers=bearer_headers(),
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()

            reel_id = data.get("id")
            if not reel_id:
                return f"Unexpected response: {data}"

            # Step 2 (optional): Upload image
            if image_url:
                img_bytes, img_name = await load_file(client, image_url)
                ext = img_name.rsplit(".", 1)[-1].lower() if "." in img_name else "jpg"
                mime = f"image/{ext}"
                await client.post(
                    f"{REEL_BASE_URL}/api/reels/{reel_id}/upload-image",
                    files={"image": (img_name, img_bytes, mime)},
                    headers=bearer_headers(),
                    timeout=30,
                )

            # Step 3: Trigger rendering
            gen = await client.post(
                f"{REEL_BASE_URL}/api/reels/{reel_id}/generate",
                headers=bearer_headers(),
                timeout=30,
            )
            gen.raise_for_status()

        # Step 4: Poll briefly — stay under MCP client tool-call timeouts
        status_url = f"{REEL_BASE_URL}/api/reels/{reel_id}"
        for _ in range(QUICK_POLL_ITERATIONS):
            await asyncio.sleep(5)
            st = await client.get(status_url, headers=bearer_headers(), timeout=15)
            if st.status_code == 200:
                st_data = st.json()
                status = str(st_data.get("status", "")).lower()
                progress = st_data.get("progress_percent", 0)

                if status == "completed" or progress >= 100:
                    video_url = st_data.get("video_url", "")
                    if video_url:
                        return f"Reel ready!\nVideo: {video_url}\nReel ID: {reel_id}"
                    # Fall back to history if video_url not in status response
                    break

                if status in ("failed", "error"):
                    return f"Reel generation failed: {st_data.get('error_message') or st_data}"

        # Step 5: Fetch from history to get video_url
        hist = await client.get(
            f"{REEL_BASE_URL}/api/reels/history",
            headers=bearer_headers(),
            timeout=15,
        )
        if hist.status_code == 200:
            items = hist.json().get("items", [])
            for item in items:
                if str(item.get("id")) == str(reel_id):
                    video_url = item.get("video_url", "")
                    if video_url:
                        return f"Reel ready!\nVideo: {video_url}\nReel ID: {reel_id}"

        return (
            f"Still generating (this can take a few minutes). "
            f"reel_id={reel_id} — call this tool again with that reel_id "
            f"to pick up right where this left off."
        )
