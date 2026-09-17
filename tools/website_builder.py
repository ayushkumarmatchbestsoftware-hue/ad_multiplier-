import asyncio
import httpx
from config import WEBBUILDER_BASE_URL
from tools.utils import bearer_headers, check_credits_guard, load_file, QUICK_POLL_ITERATIONS


async def get_website_history(limit: int = 5) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{WEBBUILDER_BASE_URL}/history",
            headers=bearer_headers(),
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items", r.json() if isinstance(r.json(), list) else [])
        if not items:
            return "No websites found in history."

        lines = [f"Last {min(limit, len(items))} website(s):\n"]
        for site in items[:limit]:
            name = site.get("site_name") or site.get("website_id", "Unnamed")
            final_url = site.get("final_url", "")
            preview_url = site.get("preview_url", "")
            status = site.get("status", "")
            lines.append(f"• {name} [{status}]")
            if final_url:
                lines.append(f"  Live: {final_url}")
            if preview_url:
                if not preview_url.startswith("http"):
                    preview_url = f"{WEBBUILDER_BASE_URL}{preview_url}"
                lines.append(f"  Preview: {preview_url}")
        return "\n".join(lines)


async def build_website(
    prompt: str = "",
    industry: str = "",
    pages: str = "",
    palette: str = "",
    logo_url: str = "",
    deploy: bool = False,
    job_id: str = "",
) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        website_id = ""

        if not job_id:
            if not prompt or not industry or not pages:
                return "Please provide prompt, industry, and pages to build a website."

            error = await check_credits_guard(client)
            if error:
                return error

            page_list = [p.strip() for p in pages.split(",") if p.strip()]

            # Use files-as-tuples to force multipart/form-data even for text fields.
            # httpx only sends multipart when `files` is present; text-only `data`
            # sends as application/x-www-form-urlencoded which this API rejects.
            multipart = [
                ("prompt", (None, prompt)),
                ("industry", (None, industry)),
            ]
            for page in page_list:
                multipart.append(("pages", (None, page)))
            if palette:
                multipart.append(("palette", (None, palette)))

            if logo_url:
                logo_bytes, logo_name = await load_file(client, logo_url)
                multipart.append(("logo", (logo_name, logo_bytes, "image/png")))

            r = await client.post(
                f"{WEBBUILDER_BASE_URL}/generate",
                files=multipart,
                headers=bearer_headers(),
                timeout=60,
            )
            r.raise_for_status()
            result = r.json()

            job_id = result.get("job_id")
            website_id = result.get("website_id", "")
            if not job_id:
                return f"Unexpected response: {result}"

        # Poll briefly — stay under MCP client tool-call timeouts
        status_url = f"{WEBBUILDER_BASE_URL}/job-status/{job_id}"
        for _ in range(QUICK_POLL_ITERATIONS):
            await asyncio.sleep(5)
            st = await client.get(status_url, headers=bearer_headers(), timeout=15)
            if st.status_code == 200:
                st_data = st.json()
                status = str(st_data.get("status", "")).lower()
                website_id = st_data.get("website_id", website_id)

                if status == "completed":
                    raw_preview = st_data.get("preview_url", "")
                    # Make preview URL absolute if it's a relative path
                    if raw_preview and not raw_preview.startswith("http"):
                        preview_url = f"{WEBBUILDER_BASE_URL}{raw_preview}"
                    else:
                        preview_url = raw_preview or f"{WEBBUILDER_BASE_URL}/preview/{website_id}/index.html"

                    if deploy:
                        dep = await client.post(
                            f"{WEBBUILDER_BASE_URL}/deploy",
                            json={"website_id": website_id},
                            headers=bearer_headers(),
                            timeout=60,
                        )
                        if dep.status_code == 200:
                            dep_data = dep.json()
                            live_url = (
                                dep_data.get("final_url")
                                or dep_data.get("url")
                                or dep_data.get("deployment_url")
                                or dep_data.get("vercel_url", "")
                            )
                            lines = ["Website built and deployed!"]
                            if live_url:
                                lines.append(f"Live URL: {live_url}")
                            lines.append(f"Preview: {preview_url}")
                            lines.append(f"Website ID: {website_id}")
                            return "\n".join(lines)

                    lines = ["Website generated!"]
                    lines.append(f"Preview: {preview_url}")
                    lines.append(f"Website ID: {website_id}")
                    return "\n".join(lines)

                if status in ("failed", "error"):
                    return f"Generation failed: {st_data}"

        return (
            f"Still generating (this can take a few minutes). "
            f"job_id={job_id} — call this tool again with that job_id "
            f"to pick up right where this left off."
        )
