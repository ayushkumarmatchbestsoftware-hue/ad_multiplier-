import asyncio
import httpx
from config import MIXBOARD_BASE_URL, USER_ID
from tools.utils import bearer_headers, check_credits_guard, QUICK_POLL_ITERATIONS


async def create_mixboard(command: str = "", num_images: int = 8, board_id: str = "") -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        if not board_id:
            if not command:
                return "Please provide a creative prompt (command) to generate a mixboard."

            error = await check_credits_guard(client)
            if error:
                return error

            # Step 1: Create mixboard
            r = await client.post(
                f"{MIXBOARD_BASE_URL}/api/mixboards/",
                json={"command": command, "user_id": USER_ID, "num_images": num_images},
                headers=bearer_headers(),
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()

            board_id = data.get("id")
            if not board_id:
                return f"Unexpected response: {data}"

        # Step 2: Poll briefly — stay under MCP client tool-call timeouts
        progress_url = f"{MIXBOARD_BASE_URL}/api/mixboards/{board_id}/progress"
        completed_progress = False
        for _ in range(QUICK_POLL_ITERATIONS):
            await asyncio.sleep(5)
            pr = await client.get(progress_url, headers=bearer_headers(), timeout=15)
            if pr.status_code == 200:
                if pr.json().get("progress_percent", 0) >= 100:
                    completed_progress = True
                    break

        if not completed_progress:
            return (
                f"Still generating (this can take a few minutes). "
                f"board_id={board_id} — call this tool again with that board_id "
                f"to pick up right where this left off."
            )

        # Step 3: Fetch board with images
        board_r = await client.get(
            f"{MIXBOARD_BASE_URL}/api/mixboards/{board_id}",
            headers=bearer_headers(),
            timeout=15,
        )
        board_r.raise_for_status()
        images = board_r.json().get("images", [])
        completed = [img for img in images if img.get("status") == "completed"]

        if not completed:
            return f"Mixboard created (id: {board_id}) but no images returned yet."

        lines = [f"Mixboard ready! {len(completed)} images generated:\n"]
        for i, img in enumerate(completed, 1):
            img_url = img.get("image_url", "")
            if img_url:
                lines.append(f"Image {i}: {img_url}")

        return "\n".join(lines)
