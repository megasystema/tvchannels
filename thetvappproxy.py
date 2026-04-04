#!/usr/bin/env python3
"""
TheTVApp Local Proxy Server  (country + category organized + Jellyfin helpers)
───────────────────────────────────────────────────────────────────────────────
- Organizes channels as "Country | Category" groups
- Assigns channel numbers (tvg-chno) by group for Jellyfin navigation
- Serves playlist dynamically based on host
- Auto-refreshes channel list every N minutes (default 10)
- Scrapes specific sports sub-categories
- Merges custom .m3u file via --custom
- Optional EPG via --epg
- Optional Jellyfin auto-refresh via --jf-url / --jf-api-key / --jf-task-id
"""

import asyncio, re, time, argparse, threading
from pathlib import Path
from datetime import datetime
from collections import Counter
import os

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, request

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://thetvapp.to/",
}
BASE_URL      = "https://thetvapp.to"
PORT = int(os.environ.get("PORT", "8087"))
CACHE_TTL     = 300           # token cache seconds
REFRESH_EVERY = 10            # minutes (updated to 10 per request)

app      = Flask(__name__)
CHANNELS = []
CUSTOM   = []
TOKEN_CACHE: dict[str, tuple[str, float]] = {}

LAST_REFRESH = {"time": None, "next": None}
CUSTOM_FILE  = {"path": ""}
EPG_URL      = {"url": ""}

# Jellyfin auto-refresh config (optional)
JELLYFIN = {
    "url": "",
    "api_key": "",
    "task_id": "",
}

# ── Sports Sub-categories to Scrape ───────────────────────────────────────────
SPORTS_CATEGORIES = {
    "MLB": "/mlb",
    "NBA": "/nba",
    "NHL": "/nhl",
    "NFL": "/nfl",
    "NCAAF": "/ncaaf",
    "NCAAB": "/ncaab",
    "Soccer": "/soccer",
    "PPV": "/ppv"
}

# ── Country code → full name map ──────────────────────────────────────────────
COUNTRY_MAP = {
    "us": "USA", "mx": "Mexico", "do": "Dominican Republic",
    "pr": "Puerto Rico", "co": "Colombia", "ve": "Venezuela",
    "ar": "Argentina", "cl": "Chile", "pe": "Peru", "ec": "Ecuador",
    "bo": "Bolivia", "py": "Paraguay", "uy": "Uruguay", "gt": "Guatemala",
    "hn": "Honduras", "sv": "El Salvador", "ni": "Nicaragua", "cr": "Costa Rica",
    "pa": "Panama", "cu": "Cuba", "ht": "Haiti", "br": "Brazil",
    "es": "Spain", "fr": "France", "it": "Italy", "de": "Germany",
    "gb": "United Kingdom", "pt": "Portugal", "nl": "Netherlands",
    "za": "South Africa", "ng": "Nigeria", "gh": "Ghana",
    "au": "Australia", "ca": "Canada", "at": "Austria",
    "international": "International",
}

# ── Pluto TV channel name → category map ─────────────────────────────────────
PLUTO_CATEGORY_MAP = {
    "cnn": "News", "fox news": "News", "msnbc": "News", "bloomberg": "News",
    "nbc news": "News", "abc news": "News", "cbs news": "News",
    "sky news": "News", "euronews": "News", "france 24": "News",
    "al jazeera": "News", "telemundo news": "News", "univision news": "News",
    "espn": "Sports", "nfl": "Sports", "nba": "Sports", "mlb": "Sports",
    "nhl": "Sports", "fox sports": "Sports", "beinsports": "Sports",
    "stadium": "Sports", "fight": "Sports", "wrestling": "Sports",
    "motor": "Sports", "racing": "Sports",
    "movies": "Movies", "cinema": "Movies", "film": "Movies",
    "horror": "Movies", "comedy movies": "Movies", "action movies": "Movies",
    "thriller": "Movies", "drama movies": "Movies", "hallmark": "Movies",
    "lifetime": "Movies", "amc": "Movies", "tcm": "Movies",
    "nickelodeon": "Kids", "cartoon": "Kids", "disney": "Kids",
    "nick jr": "Kids", "baby": "Kids", "kid": "Kids",
    "music": "Music", "mtv": "Music", "vh1": "Music", "bet": "Music",
    "discovery": "Documentary", "history": "Documentary", "nat geo": "Documentary",
    "science": "Documentary", "animal": "Documentary", "nature": "Documentary",
    "crime": "Documentary", "investigation": "Documentary",
    "comedy": "Entertainment", "reality": "Entertainment", "bravo": "Entertainment",
    "e!": "Entertainment", "pop": "Entertainment", "tbs": "Entertainment",
    "tnt": "Entertainment", "usa network": "Entertainment",
    "en español": "Spanish", "telenovela": "Spanish", "novela": "Spanish",
    "univision": "Spanish", "telemundo": "Spanish", "galavision": "Spanish",
    "estrella": "Spanish", "unimas": "Spanish",
}


# ── Helpers: country & category ───────────────────────────────────────────────

def normalize_category(raw: str, channel_name: str = "", tvg_id: str = "") -> str:
    raw_lower  = raw.strip().lower()
    name_lower = channel_name.strip().lower()

    is_pluto = "plutotv" in tvg_id.lower() or "pluto" in tvg_id.lower()
    if is_pluto or not raw_lower or raw_lower in ("pluto tv", "plutotv", "pluto"):
        for keyword, category in PLUTO_CATEGORY_MAP.items():
            if keyword in name_lower:
                return category
        return "Pluto TV"

    if any(x in raw_lower for x in ["sport", "deport", "futbol", "football", "soccer", "baseball", "nba", "nfl", "nhl"]):
        return "Sports"
    if any(x in raw_lower for x in ["news", "noticias", "noticiero"]):
        return "News"
    if any(x in raw_lower for x in ["movie", "pelicula", "cine", "film"]):
        return "Movies"
    if any(x in raw_lower for x in ["kid", "child", "infantil", "cartoon", "animation"]):
        return "Kids"
    if any(x in raw_lower for x in ["music", "musica"]):
        return "Music"
    if any(x in raw_lower for x in ["religious", "religion", "religioso", "faith", "christian", "gospel"]):
        return "Religious"
    if any(x in raw_lower for x in ["entertain", "entretenimiento", "variety"]):
        return "Entertainment"
    if "general" in raw_lower:
        return "General"
    if any(x in raw_lower for x in ["cultur", "document", "documental", "history"]):
        return "Documentary"

    return raw.strip().title() if raw.strip() else "General"


def extract_country_from_tvgid(tvg_id: str) -> str:
    m = re.search(r"\.([a-z]{2})(?:@|$)", tvg_id.lower())
    if m:
        code = m.group(1)
        return COUNTRY_MAP.get(code, code.upper())
    return "International"


def make_group(country: str, category: str) -> str:
    return f"{country} | {category}"


# ── 1. Scrape TheTVApp channel list ──────────────────────────────────────────

def fetch_channels() -> list[dict]:
    entries, seen = [], set()

    try:
        resp = requests.get(BASE_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Canales regulares de TheTVApp (entretenimiento / live TV)
        for a in soup.find_all("a", href=re.compile(r"^/tv/[^/]+-live-stream/")):
            href = a["href"]
            if href in seen:
                continue
            seen.add(href)
            raw  = a.get_text(" ", strip=True)
            name = re.sub(r"^\d+\s*\.\s*", "", raw).strip() or href.split("/")[2]
            slug = href.strip("/").split("/")[-1]
            entries.append({
                "name": name,
                "url":  BASE_URL + href,
                "slug": slug,
                "group": "1 tv app entretenimiento",
                "logo": "",
                "tvg_id": "",
                "custom": False,
            })

        # Eventos / deportes de TheTVApp
        for a in soup.find_all("a", href=re.compile(r"^/event/")):
            href = a["href"]
            if href in seen:
                continue
            seen.add(href)
            raw  = a.get_text(" ", strip=True)
            name = re.sub(r"^\d+\s*\.\s*", "", raw).strip() or href.split("/")[2]
            slug = href.strip("/").split("/")[-1]
            entries.append({
                "name": name,
                "url":  BASE_URL + href,
                "slug": slug,
                "group": "2 tv app deportes",
                "logo": "",
                "tvg_id": "",
                "custom": False,
            })
    except Exception as e:
        print(f"[!] Error fetching main page: {e}")

    # Scrape Specific Sports Sub-Categories
    for sport_name, sport_path in SPORTS_CATEGORIES.items():
        try:
            target_url = BASE_URL + sport_path
            resp = requests.get(target_url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=re.compile(r"^/(?:event|tv)/")):
                href = a["href"]
                if href in seen:
                    continue
                seen.add(href)
                raw  = a.get_text(" ", strip=True)
                name = re.sub(r"^\d+\s*\.\s*", "", raw).strip() or href.split("/")[-1]
                slug = href.strip("/").split("/")[-1]
                
                entries.append({
                    "name": f"[{sport_name}] {name}",
                    "url":  BASE_URL + href,
                    "slug": slug,
                    "group": f"2 tv app deportes | {sport_name}",
                    "logo": "",
                    "tvg_id": "",
                    "custom": False,
                })
        except Exception as e:
            print(f"[!] Error fetching {sport_name} category: {e}")

    return entries


def trigger_jellyfin_refresh():
    """Trigger Jellyfin 'Refresh Guide' scheduled task if configured."""
    jf_url    = JELLYFIN["url"].rstrip("/")
    api_key   = JELLYFIN["api_key"]
    task_id   = JELLYFIN["task_id"]

    if not (jf_url and api_key and task_id):
        return

    try:
        endpoint = f"{jf_url}/ScheduledTasks/{task_id}/Trigger"
        resp = requests.post(
            endpoint,
            headers={"X-Emby-Token": api_key},
            timeout=10,
        )
        if resp.status_code == 204 or resp.status_code == 200:
            print(f"[JF] Triggered Jellyfin guide refresh ({endpoint})")
        else:
            print(f"[JF] Refresh request returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[JF] Failed to trigger Jellyfin refresh: {e}")


def refresh_channels():
    global CHANNELS
    try:
        print(f"\n[↻] Refreshing at {datetime.now().strftime('%H:%M:%S')} ...")
        new_channels = fetch_channels()
        new_slugs = {ch["slug"] for ch in new_channels}
        for s in [s for s in list(TOKEN_CACHE.keys()) if s not in new_slugs]:
            del TOKEN_CACHE[s]
        CHANNELS = new_channels
        LAST_REFRESH["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LAST_REFRESH["next"] = f"in {REFRESH_EVERY} min"
        print(f"[+] {len(CHANNELS)} TheTVApp channels. Next refresh {LAST_REFRESH['next']}.")

        # After proxy refreshes, ask Jellyfin to refresh guide (and channels)
        trigger_jellyfin_refresh()
    except Exception as e:
        print(f"[!] Refresh failed: {e}")


def start_refresh_timer():
    def loop():
        while True:
            time.sleep(REFRESH_EVERY * 60)
            refresh_channels()
    threading.Thread(target=loop, daemon=True).start()


# ── 2. Parse custom .m3u (handles #EXTVLCOPT, country + Pluto TV detection) ──

def parse_custom_m3u(filepath: str) -> list[dict]:
    path = Path(filepath)
    if not path.exists():
        print(f"[!] Custom file not found: {filepath}")
        return []

    entries = []
    lines   = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("#EXTINF"):
            name_match  = re.search(r",(.+)$", line)
            name        = name_match.group(1).strip() if name_match else "Unknown"

            tvgid_match = re.search(r'tvg-id="([^"]*)"', line)
            tvg_id      = tvgid_match.group(1) if tvgid_match else ""

            logo_match  = re.search(r'tvg-logo="([^"]*)"', line)
            logo        = logo_match.group(1) if logo_match else ""

            group_match = re.search(r'group-title="([^"]*)"', line)
            raw_group   = group_match.group(1).split(";")[0].strip() if group_match else ""

            country  = extract_country_from_tvgid(tvg_id) if tvg_id else "International"
            category = normalize_category(raw_group, channel_name=name, tvg_id=tvg_id)
            # CAMBIO: agrupar solo por país (sin categoría)
            group    = country

            i += 1
            stream_url = None
            while i < len(lines):
                nl = lines[i].strip()
                if not nl:
                    i += 1
                    continue
                if nl.startswith("#EXT"):
                    i += 1
                    continue
                if nl.startswith("http") or nl.startswith("rtmp"):
                    stream_url = nl
                    break
                break

            if stream_url:
                slug = re.sub(
                    r"[^a-z0-9]+",
                    "-",
                    name.lower()
                        .replace("ñ", "n").replace("é", "e").replace("á", "a")
                        .replace("ó", "o").replace("ú", "u").replace("í", "i"),
                ).strip("-")
                entries.append({
                    "name": name,
                    "url": stream_url,
                    "slug": f"custom-{slug}",
                    "group": group,
                    "logo": logo,
                    "tvg_id": tvg_id,
                    "custom": True,
                    "stream_url": stream_url,
                })
        i += 1

    groups = Counter(e["group"] for e in entries)
    print("\n[Groups detected in custom playlist]")
    for grp, count in sorted(groups.items()):
        print(f"  {grp}: {count} channels")

    return entries


# ── 3. Playwright token fetch ─────────────────────────────────────────────────

async def _get_stream_url(page_url: str) -> str | None:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx  = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await ctx.new_page()
        found: list[str] = []

        async def intercept(req):
            if ".m3u8" in req.url and not found:
                found.append(req.url)

        page.on("request", intercept)

        try:
            await page.goto(page_url, wait_until="networkidle", timeout=20_000)
            deadline = time.time() + 10
            while not found and time.time() < deadline:
                await asyncio.sleep(0.3)
        except Exception:
            pass
        finally:
            await browser.close()

    return found[0] if found else None


def get_stream_url(page_url: str, slug: str) -> str | None:
    cached_url, cached_at = TOKEN_CACHE.get(slug, (None, 0))
    if cached_url and (time.time() - cached_at) < CACHE_TTL:
        return cached_url
    url = asyncio.run(_get_stream_url(page_url))
    if url:
        TOKEN_CACHE[slug] = (url, time.time())
    return url


# ── 4. HTML Template (unchanged behaviour) ────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="REFRESH_SEC">
  <title>TheTVApp Proxy</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:sans-serif;background:#0f0f0f;color:#eee;padding:2rem}
    h1{color:#00d4ff;margin-bottom:1rem}
    .info{background:#1a1a2e;padding:1rem 1.5rem;border-radius:8px;margin-bottom:1rem;border:1px solid #0e4d6e}
    .info code{color:#00d4ff;font-size:1rem}
    .meta{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1.5rem;font-size:.84rem;color:#aaa}
    .meta span{background:#1a1a2e;padding:.4rem .9rem;border-radius:6px}
    .meta span b{color:#eee}
    .meta a{background:#0e4d6e;color:#7dd3fc;padding:.4rem .9rem;border-radius:6px;text-decoration:none;font-weight:bold}
    .meta a:hover{background:#1a6e8e}
    hr{display:block;height:1px;border:0;border-top:1px solid #1a1a2e;margin:1em 0;padding:0}
    .error-details{margin-top:0.5em;max-width:600px}
    .filters{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1rem}
    .filters input,.filters select{background:#1a1a2e;border:1px solid #0e4d6e;color:#eee;
      padding:.45rem .9rem;border-radius:6px;font-size:.9rem}
    .filters input{width:260px}
    table{width:100%;border-collapse:collapse}
    th{background:#1a1a2e;padding:.6rem 1rem;text-align:left;color:#00d4ff;font-size:.82rem;text-transform:uppercase;letter-spacing:.05em}
    td{padding:.45rem 1rem;border-bottom:1px solid #1a1a1a;font-size:.88rem}
    tr:hover td{background:#1a1a2e}
    a.play{color:#00d4ff;text-decoration:none;font-weight:bold}
    a.play:hover{text-decoration:underline}
    .grp{background:#1a1a2e;color:#94a3b8;padding:.15rem .55rem;border-radius:4px;font-size:.75rem;white-space:nowrap}
    .logo{width:26px;height:26px;object-fit:contain;border-radius:3px;vertical-align:middle}
    code{font-size:.72rem;color:#555}
  </style>
</head>
<body>
  <h1>&#128250; TheTVApp Local Proxy</h1>
  <div class="info">
    Playlist URL for your IPTV app (Jellyfin / TiviMate / Kodi):
    <code>HOST_PLACEHOLDER/playlist.m3u</code>
  </div>
  <div class="meta">
    <span>Total: <b>COUNT_PLACEHOLDER</b></span>
    <span>TheTVApp: <b>TVAPP_COUNT</b></span>
    <span>Custom: <b>CUSTOM_COUNT</b></span>
    <span>Last refresh: <b>LAST_REFRESH</b></span>
    <span>Next: <b>NEXT_REFRESH</b></span>
    <a href="/refresh">&#8635; Refresh Now</a>
  </div>
  <div class="filters">
    <input type="text" id="search" placeholder="&#128269; Search channel name..." oninput="applyFilters()">
    <select id="countryFilter" onchange="applyFilters()">
      <option value="">All Countries</option>
      COUNTRY_OPTIONS
    </select>
    <select id="catFilter" onchange="applyFilters()">
      <option value="">All Categories</option>
      CAT_OPTIONS
    </select>
  </div>
  <table id="tbl">
    <thead>
      <tr><th>#</th><th>Logo</th><th>Channel</th><th>Group</th><th>Play</th><th>URL</th></tr>
    </thead>
    <tbody>
ROWS_PLACEHOLDER
    </tbody>
  </table>
  <script>
    function applyFilters(){
      var q  = document.getElementById('search').value.toLowerCase();
      var co = document.getElementById('countryFilter').value.toLowerCase();
      var ca = document.getElementById('catFilter').value.toLowerCase();
      document.querySelectorAll('#tbl tbody tr').forEach(function(r){
        var txt  = r.textContent.toLowerCase();
        var grp  = (r.dataset.group||'').toLowerCase();
        var show = (!q || txt.includes(q))
                && (!co || grp.startsWith(co))
                && (!ca || grp.includes('| ' + ca.toLowerCase()));
        r.style.display = show ? '' : 'none';
      });
    }
  </script>
</body>
</html>"""


# ── 5. Flask routes ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    all_channels = CHANNELS + CUSTOM
    host_url = request.host_url.rstrip("/")

    countries  = sorted(set(ch["group"].split(" | ")[0] for ch in all_channels))
    categories = sorted(set(ch["group"].split(" | ")[1] for ch in all_channels if " | " in ch["group"]))

    country_opts = "\n".join(f'<option value="{c}">{c}</option>' for c in countries)
    cat_opts     = "\n".join(f'<option value="{c}">{c}</option>' for c in categories)

    sorted_channels = sorted(all_channels, key=lambda x: (x["group"], x["name"]))

    rows = ""
    for i, ch in enumerate(sorted_channels, 1):
        stream_link = ch["stream_url"] if ch.get("custom") else f"{host_url}/stream/{ch['slug']}"
        logo        = ch.get("logo", "")
        logo_html   = f'<img class="logo" src="{logo}" alt="" loading="lazy">' if logo else ""
        group       = ch.get("group", "")

        rows += (
            f'<tr data-group="{group}">'
            f"<td>{i}</td>"
            f"<td>{logo_html}</td>"
            f"<td>{ch['name']}</td>"
            f"<td><span class='grp'>{group}</span></td>"
            f"<td><a class='play' href='{stream_link}' target='_blank'>&#9654; Play</a></td>"
            f"<td><code>{stream_link}</code></td>"
            f"</tr>\n"
        )

    html = (
        HTML_TEMPLATE
        .replace("HOST_PLACEHOLDER",  host_url)
        .replace("COUNT_PLACEHOLDER", str(len(all_channels)))
        .replace("TVAPP_COUNT",       str(len(CHANNELS)))
        .replace("CUSTOM_COUNT",      str(len(CUSTOM)))
        .replace("LAST_REFRESH",      LAST_REFRESH["time"] or "N/A")
        .replace("NEXT_REFRESH",      LAST_REFRESH["next"] or "N/A")
        .replace("REFRESH_SEC",       str(REFRESH_EVERY * 60))
        .replace("COUNTRY_OPTIONS",   country_opts)
        .replace("CAT_OPTIONS",       cat_opts)
        .replace("ROWS_PLACEHOLDER",  rows)
    )
    return Response(html, mimetype="text/html")


@app.route("/playlist.m3u")
def playlist():
    all_channels = sorted(CHANNELS + CUSTOM, key=lambda x: (x["group"], x["name"]))
    epg          = EPG_URL["url"]
    host_url     = request.host_url.rstrip("/")

    header = "#EXTM3U"
    if epg:
        header += f' url-tvg="{epg}" tvg-shift="-5"'
    lines = [header]

    # Assign channel numbers per group: each group gets its own 100-block
    group_order = []
    for ch in all_channels:
        grp = ch.get("group", "General")
        if grp not in group_order:
            group_order.append(grp)

    group_base = {grp: (i + 1) * 100 for i, grp in enumerate(group_order)}
    counters   = {grp: 0 for grp in group_order}

    for ch in all_channels:
        group     = ch.get("group", "General")
        base      = group_base[group]
        counters[group] += 1
        chno      = base + counters[group]

        stream_url = ch["stream_url"] if ch.get("custom") else f"{host_url}/stream/{ch['slug']}"
        tvg_id     = ch.get("tvg_id", "")
        logo       = ch.get("logo", "")
        name       = ch["name"]

        lines.append(
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
            f'tvg-logo="{logo}" group-title="{group}" tvg-chno="{chno}",{name}'
        )
        lines.append(stream_url)

    return Response(
        "\n".join(lines),
        mimetype="application/x-mpegurl",
        headers={"Content-Disposition": "inline; filename=playlist.m3u"}
    )


@app.route("/stream/<slug>")
def stream(slug):
    ch = next((c for c in CHANNELS if c["slug"] == slug), None)
    if not ch:
        return "Channel not found", 404
    print(f"[->] Token request: {ch['name']}")
    url = get_stream_url(ch["url"], slug)
    if not url:
        return "Stream unavailable", 503
    return redirect(url)


@app.route("/refresh")
def manual_refresh():
    refresh_channels()
    if CUSTOM_FILE["path"]:
        global CUSTOM
        CUSTOM = parse_custom_m3u(CUSTOM_FILE["path"])
    return redirect("/")


# ── 6. Entry point ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="TheTVApp Local Proxy Server")
    ap.add_argument("--port",    type=int, default=8087, help="Proxy port (default 8087)")
    ap.add_argument("--custom",  default="", help="Path to custom .m3u file")
    ap.add_argument("--refresh", type=int, default=10, help="Channel list refresh interval in minutes")
    ap.add_argument("--epg",     default="", help="EPG XMLTV URL")
    ap.add_argument("--jf-url",      default="", help="Jellyfin base URL (e.g. http://localhost:8096)")
    ap.add_argument("--jf-api-key",  default="", help="Jellyfin API key with admin rights")
    ap.add_argument("--jf-task-id",  default="", help="Jellyfin 'Refresh Guide' scheduled task ID")
    args = ap.parse_args()

    CUSTOM_FILE["path"] = args.custom
    EPG_URL["url"]      = args.epg

    JELLYFIN["url"]     = args.jf_url
    JELLYFIN["api_key"] = args.jf_api_key
    JELLYFIN["task_id"] = args.jf_task_id

    global PORT, REFRESH_EVERY, CHANNELS, CUSTOM
    PORT          = args.port
    REFRESH_EVERY = args.refresh

    print("[*] Fetching channel list from thetvapp.to ...")
    CHANNELS = fetch_channels()
    LAST_REFRESH["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LAST_REFRESH["next"] = f"in {REFRESH_EVERY} min"
    print(f"[+] {len(CHANNELS)} TheTVApp channels loaded.")

    if args.custom:
        CUSTOM = parse_custom_m3u(args.custom)
        print(f"[+] {len(CUSTOM)} custom channels loaded from {args.custom}")

    if args.epg:
        print(f"[+] EPG source: {args.epg}")

    if JELLYFIN["url"]:
        print(f"[+] Jellyfin auto-refresh enabled for {JELLYFIN['url']}")

    print(f"\n[OK] Total channels : {len(CHANNELS) + len(CUSTOM)}")
    print(f"[OK] Auto-refresh   : every {REFRESH_EVERY} minutes")
    print(f"[OK] Browser UI     : http://localhost:{PORT}/")
    print(f"[OK] Playlist URL   : http://localhost:{PORT}/playlist.m3u\n")

    start_refresh_timer()
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()