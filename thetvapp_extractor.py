#!/usr/bin/env python3
"""
TheTVApp M3U8 Extractor  (fixed)
─────────────────────────────────
Scrapes https://thetvapp.to for Live-TV channels AND sports events,
then tries to pull the .m3u8 stream URL from each page.

NOTE: TheTVApp requires a paid subscription to stream; unauthenticated
      pages do not expose the raw m3u8.  The script will:
        1) Build a full channel/event list from the homepage.
        2) Try to find an m3u8/stream URL inside each page's HTML/JS.
        3) Write whatever it finds to an M3U file.
      For authenticated extraction use --method playwright and supply
      --username / --password so the headless browser can log in.

Requirements:
    pip install requests beautifulsoup4
    pip install playwright && playwright install chromium   # for --method playwright
"""

import asyncio, json, re, time, argparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://thetvapp.to/",
}
BASE_URL = "https://thetvapp.to"


# ── 1. Scrape channel + event links from homepage ────────────────────────────

def fetch_all_links() -> list[dict]:
    resp = requests.get(BASE_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    entries = []
    seen = set()

    # Live-TV channels  →  /tv/<slug>/
    for a in soup.find_all("a", href=re.compile(r"^/tv/[^/]+-live-stream/")):
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)
        raw = a.get_text(" ", strip=True)
        name = re.sub(r"^\d+\s*\.\s*", "", raw).strip() or href.split("/")[2]
        entries.append({"name": name, "url": BASE_URL + href, "group": "Live TV"})

    # Sports events  →  /event/<slug>/
    for a in soup.find_all("a", href=re.compile(r"^/event/")):
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)
        raw = a.get_text(" ", strip=True)
        name = re.sub(r"^\d+\s*\.\s*", "", raw).strip() or href.split("/")[2]
        entries.append({"name": name, "url": BASE_URL + href, "group": "Sports"})

    return entries


# ── 2a. HTTP-only extraction ──────────────────────────────────────────────────

M3U8_RE = re.compile(
    r'(https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?)', re.IGNORECASE
)

def extract_http(page_url: str) -> str | None:
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
    except Exception:
        return None
    if r.status_code != 200:
        return None

    # Direct regex over full HTML
    m = M3U8_RE.search(r.text)
    if m:
        return m.group(1)

    # JSON blobs
    for block in re.findall(r"\{[^{}]{0,3000}\}", r.text):
        try:
            data = json.loads(block)
            for key in ("url", "src", "source", "stream", "file", "hls", "m3u8"):
                val = data.get(key, "")
                if isinstance(val, str) and ".m3u8" in val.lower():
                    return val
        except Exception:
            pass

    return None


# ── 2b. Playwright extraction (handles JS-rendered tokens + login) ────────────

async def _pw_extract(page_url: str, username: str, password: str) -> str | None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await ctx.new_page()

        found: list[str] = []

        async def intercept(request):
            if ".m3u8" in request.url and not found:
                found.append(request.url)

        page.on("request", intercept)

        # Log in if credentials supplied
        if username and password:
            try:
                await page.goto(BASE_URL + "/login", timeout=15_000)
                await page.fill('input[name="email"]',    username)
                await page.fill('input[name="password"]', password)
                await page.click('button[type="submit"]')
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception as e:
                print(f"  [login failed: {e}]", end=" ")

        try:
            await page.goto(page_url, wait_until="networkidle", timeout=20_000)
            deadline = time.time() + 10
            while not found and time.time() < deadline:
                await asyncio.sleep(0.4)
        except Exception:
            pass
        finally:
            await browser.close()

    return found[0] if found else None


def extract_playwright(page_url: str, username: str, password: str) -> str | None:
    return asyncio.run(_pw_extract(page_url, username, password))


# ── 3. Build M3U ──────────────────────────────────────────────────────────────

def build_m3u(entries: list[dict]) -> str:
    lines = ["#EXTM3U"]
    for ch in entries:
        url = ch.get("stream_url", "")
        if url:
            lines.append(
                f'#EXTINF:-1 tvg-name="{ch["name"]}" '
                f'group-title="{ch.get("group","TheTVApp")}",{ch["name"]}'
            )
            lines.append(url)
    return "\n".join(lines)


# ── 4. CLI ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="TheTVApp M3U8 Extractor")
    ap.add_argument("--method", choices=["http", "playwright"], default="http")
    ap.add_argument("--output",   default="thetvapp_playlist.m3u")
    ap.add_argument("--delay",    type=float, default=0.8,
                    help="Seconds between requests (default 0.8)")
    ap.add_argument("--username", default="", help="TheTVApp login email (playwright only)")
    ap.add_argument("--password", default="", help="TheTVApp login password (playwright only)")
    ap.add_argument("--limit",    type=int, default=0,
                    help="Max channels to process (0 = all)")
    args = ap.parse_args()

    print("[*] Fetching channel list ...")
    entries = fetch_all_links()
    if args.limit:
        entries = entries[: args.limit]
    print(f"[+] {len(entries)} entries found (Live TV + Sports).\n")

    results = []
    for i, ch in enumerate(entries, 1):
        print(f"[{i:>3}/{len(entries)}] {ch['name'][:55]:<55}", end=" ", flush=True)

        if args.method == "playwright":
            url = extract_playwright(ch["url"], args.username, args.password)
        else:
            url = extract_http(ch["url"])

        print("OK" if url else "NOT FOUND")
        results.append({**ch, "stream_url": url or ""})
        time.sleep(args.delay)

    playlist = build_m3u(results)
    out = Path(args.output)
    out.write_text(playlist, encoding="utf-8")

    found = sum(1 for r in results if r["stream_url"])
    print(f"\n[Done] {found}/{len(results)} streams saved -> {out}")
    if found == 0:
        print(
            "\n[!] 0 streams found. TheTVApp requires a paid subscription.\n"
            "    Re-run with:  --method playwright --username EMAIL --password PASS\n"
            "    to log in and intercept the live token-protected .m3u8 URLs."
        )


if __name__ == "__main__":
    main()