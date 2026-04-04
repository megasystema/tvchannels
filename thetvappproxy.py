#!/usr/bin/env python3
"""
TheTVApp Local Proxy Server  (country organized + Xtream Codes API + Jellyfin helpers)
───────────────────────────────────────────────────────────────────────────────
- TVApp channels in groups: "1 tv app entretenimiento" / "2 tv app deportes"
- Custom .m3u channels organized by country only
- Xtream Codes API endpoint for IPTV Smarters / Perfect Player
- Serves M3U playlist at /playlist.m3u
- Auto-refreshes channel list every N minutes (default 30)

Requirements:
    pip install requests beautifulsoup4 playwright flask
    playwright install chromium

Usage:
    python thetvapp_proxy.py --custom my_channels.m3u
    python thetvapp_proxy.py --custom my_channels.m3u --epg "https://epg.pw/xmltv/epg.xml"
"""

import asyncio, re, time, argparse, threading, json
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
PORT          = int(os.environ.get("PORT", "8087"))
CACHE_TTL     = 300
REFRESH_EVERY = 30

# Xtream Codes credentials (set as env vars in Railway)
XC_USER = os.environ.get("XC_USER", "admin")
XC_PASS = os.environ.get("XC_PASS", "admin")

app      = Flask(__name__)
CHANNELS = []
CUSTOM   = []
TOKEN_CACHE: dict[str, tuple[str, float]] = {}

LAST_REFRESH = {"time": None, "next": None}
CUSTOM_FILE  = {"path": ""}
EPG_URL      = {"url": ""}

JELLYFIN = {
    "url": "",
    "api_key": "",
    "task_id": "",
}

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
    "en espanol": "Spanish", "telenovela": "Spanish", "novela": "Spanish",
    "univision": "Spanish", "telemundo": "Spanish", "galavision": "Spanish",
    "estrella": "Spanish", "unimas": "Spanish",
}


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


# ── 1. Scrape TheTVApp channel list ──────────────────────────────────────────

def fetch_channels() -> list[dict]:
    resp = requests.get(BASE_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    entries, seen = [], set()

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

    return entries


def trigger_jellyfin_refresh():
    jf_url  = JELLYFIN["url"].rstrip("/")
    api_key = JELLYFIN["api_key"]
    task_id = JELLYFIN["task_id"]
    if not (jf_url and api_key and task_id):
        return
    try:
        endpoint = f"{jf_url}/ScheduledTasks/{task_id}/Trigger"
        resp = requests.post(endpoint, headers={"X-Emby-Token": api_key}, timeout=10)
        if resp.status_code in (200, 204):
            print(f"[JF] Triggered Jellyfin guide refresh ({endpoint})")
        else:
            print(f"[JF] Refresh returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[JF] Failed: {e}")


def refresh_channels():
    global CHANNELS
    try:
        print(f"\n[refresh] Refreshing at {datetime.now().strftime('%H:%M:%S')} ...")
        new_channels = fetch_channels()
        new_slugs = {ch["slug"] for ch in new_channels}
        for s in [s for s in list(TOKEN_CACHE.keys()) if s not in new_slugs]:
            del TOKEN_CACHE[s]
        CHANNELS = new_channels
        LAST_REFRESH["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LAST_REFRESH["next"] = f"in {REFRESH_EVERY} min"
        print(f"[+] {len(CHANNELS)} TheTVApp channels. Next refresh {LAST_REFRESH['next']}.")
        trigger_jellyfin_refresh()
    except Exception as e:
        print(f"[!] Refresh failed: {e}")


def start_refresh_timer():
    def loop():
        while True:
            time.sleep(REFRESH_EVERY * 60)
            refresh_channels()
    threading.Thread(target=loop, daemon=True).start()


# ── 2. Parse custom .m3u ─────────────────────────────────────────────────────

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
            group    = country  # Solo por pais

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
                    r"[^a-z0-9]+", "-",
                    name.lower()
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
    print("\n[Custom playlist groups (by country)]")
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


# ── Helper: ordered channel list ─────────────────────────────────────────────

def get_all_channels_sorted():
    return sorted(CHANNELS + CUSTOM, key=lambda x: (x["group"], x["name"]))


def get_group_order():
    all_ch = get_all_channels_sorted()
    seen = []
    for ch in all_ch:
        if ch["group"] not in seen:
            seen.append(ch["group"])
    return seen


# ── 4. HTML Template ──────────────────────────────────────────────────────────

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
    .info code{color:#00d4ff;font-size:.9rem}
    .xc-box{background:#1a2e1a;padding:1rem 1.5rem;border-radius:8px;margin-bottom:1rem;border:1px solid #0e6e2e}
    .xc-box h3{color:#00ff88;margin-bottom:.5rem;font-size:.95rem}
    .xc-box code{color:#00ff88;font-size:.85rem;display:block;margin:.2rem 0}
    .meta{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1.5rem;font-size:.84rem;color:#aaa}
    .meta span{background:#1a1a2e;padding:.4rem .9rem;border-radius:6px}
    .meta span b{color:#eee}
    .meta a{background:#0e4d6e;color:#7dd3fc;padding:.4rem .9rem;border-radius:6px;text-decoration:none;font-weight:bold}
    .meta a:hover{background:#1a6e8e}
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
    <b>M3U Playlist URL:</b><br>
    <code>M3U_URL_PLACEHOLDER</code>
  </div>
  <div class="xc-box">
    <h3>&#127381; Xtream Codes (IPTV Smarters / Perfect Player)</h3>
    <code>Host: XC_HOST_PLACEHOLDER</code>
    <code>Usuario: XC_USER_PLACEHOLDER</code>
    <code>Contrasena: XC_PASS_PLACEHOLDER</code>
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
    <select id="groupFilter" onchange="applyFilters()">
      <option value="">All Groups</option>
      GROUP_OPTIONS
    </select>
  </div>
  <table id="tbl">
    <thead>
      <tr><th>#</th><th>Logo</th><th>Channel</th><th>Group</th><th>Play</th></tr>
    </thead>
    <tbody>
ROWS_PLACEHOLDER
    </tbody>
  </table>
  <script>
    function applyFilters(){
      var q  = document.getElementById('search').value.toLowerCase();
      var gr = document.getElementById('groupFilter').value.toLowerCase();
      document.querySelectorAll('#tbl tbody tr').forEach(function(r){
        var txt = r.textContent.toLowerCase();
        var grp = (r.dataset.group||'').toLowerCase();
        r.style.display = (!q || txt.includes(q)) && (!gr || grp === gr) ? '' : 'none';
      });
    }
  </script>
</body>
</html>"""


# ── 5. Flask routes ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    all_channels = get_all_channels_sorted()
    base         = request.host_url.rstrip("/")

    groups     = list(dict.fromkeys(ch["group"] for ch in all_channels))
    group_opts = "\n".join(f'<option value="{g}">{g}</option>' for g in groups)

    rows = ""
    for i, ch in enumerate(all_channels, 1):
        stream_link = ch["stream_url"] if ch.get("custom") else f"{base}/stream/{ch['slug']}"
        logo        = ch.get("logo", "")
        logo_html   = f'<img class="logo" src="{logo}" alt="" loading="lazy">' if logo else ""
        group       = ch.get("group", "")
        rows += (
            f'<tr data-group="{group}">'
            f"<td>{i}</td><td>{logo_html}</td><td>{ch['name']}</td>"
            f"<td><span class='grp'>{group}</span></td>"
            f"<td><a class='play' href='{stream_link}' target='_blank'>&#9654; Play</a></td>"
            f"</tr>\n"
        )

    html = (
        HTML_TEMPLATE
        .replace("M3U_URL_PLACEHOLDER",  f"{base}/playlist.m3u")
        .replace("XC_HOST_PLACEHOLDER",  base)
        .replace("XC_USER_PLACEHOLDER",  XC_USER)
        .replace("XC_PASS_PLACEHOLDER",  XC_PASS)
        .replace("COUNT_PLACEHOLDER",    str(len(all_channels)))
        .replace("TVAPP_COUNT",          str(len(CHANNELS)))
        .replace("CUSTOM_COUNT",         str(len(CUSTOM)))
        .replace("LAST_REFRESH",         LAST_REFRESH["time"] or "N/A")
        .replace("NEXT_REFRESH",         LAST_REFRESH["next"] or "N/A")
        .replace("REFRESH_SEC",          str(REFRESH_EVERY * 60))
        .replace("GROUP_OPTIONS",        group_opts)
        .replace("ROWS_PLACEHOLDER",     rows)
    )
    return Response(html, mimetype="text/html")


@app.route("/playlist.m3u")
def playlist():
    all_channels = get_all_channels_sorted()
    base         = request.host_url.rstrip("/")
    epg          = EPG_URL["url"]

    header = "#EXTM3U"
    if epg:
        header += f' url-tvg="{epg}" tvg-shift="-5"'
    lines = [header]

    group_order = get_group_order()
    group_base  = {grp: (i + 1) * 100 for i, grp in enumerate(group_order)}
    counters    = {grp: 0 for grp in group_order}

    for ch in all_channels:
        group = ch.get("group", "General")
        counters[group] += 1
        chno       = group_base[group] + counters[group]
        stream_url = ch["stream_url"] if ch.get("custom") else f"{base}/stream/{ch['slug']}"
        lines.append(
            f'#EXTINF:-1 tvg-id="{ch.get("tvg_id","")}" tvg-name="{ch["name"]}" '
            f'tvg-logo="{ch.get("logo","")}" group-title="{group}" tvg-chno="{chno}",{ch["name"]}'
        )
        lines.append(stream_url)

    return Response(
        "\n".join(lines),
        mimetype="application/x-mpegurl",
        headers={"Content-Disposition": "inline; filename=playlist.m3u"}
    )


# ── 6. Xtream Codes API ───────────────────────────────────────────────────────

def xc_auth(username: str, password: str) -> bool:
    return username == XC_USER and password == XC_PASS


def xc_user_info(username: str):
    return {
        "username": username,
        "password": XC_PASS,
        "message": "",
        "auth": 1,
        "status": "Active",
        "exp_date": "9999999999",
        "is_trial": "0",
        "active_cons": "1",
        "created_at": "0",
        "max_connections": "10",
        "allowed_output_formats": ["m3u8", "ts", "rtmp"]
    }


def xc_server_info():
    return {
        "url": os.environ.get("RAILWAY_STATIC_URL", "localhost"),
        "port": "80",
        "https_port": "443",
        "server_protocol": "https",
        "rtmp_port": "1935",
        "timezone": "America/New_York",
        "timestamp_now": int(time.time()),
        "time_now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


@app.route("/player_api.php")
def player_api():
    username = request.args.get("username", "")
    password = request.args.get("password", "")
    action   = request.args.get("action", "")

    if not xc_auth(username, password):
        return Response(json.dumps({"user_info": {"auth": 0}}), mimetype="application/json")

    if not action:
        data = {
            "user_info":   xc_user_info(username),
            "server_info": xc_server_info()
        }
        return Response(json.dumps(data), mimetype="application/json")

    if action == "get_live_categories":
        groups = get_group_order()
        cats = [
            {"category_id": str(i + 1), "category_name": g, "parent_id": 0}
            for i, g in enumerate(groups)
        ]
        return Response(json.dumps(cats), mimetype="application/json")

    if action == "get_live_streams":
        base   = request.host_url.rstrip("/")
        all_ch = get_all_channels_sorted()
        groups = get_group_order()
        cat_map = {g: str(i + 1) for i, g in enumerate(groups)}

        streams = []
        for i, ch in enumerate(all_ch, 1):
            stream_url = ch["stream_url"] if ch.get("custom") else f"{base}/stream/{ch['slug']}"
            streams.append({
                "num":                  i,
                "name":                 ch["name"],
                "stream_type":          "live",
                "stream_id":            i,
                "stream_icon":          ch.get("logo", ""),
                "epg_channel_id":       ch.get("tvg_id", ""),
                "added":                str(int(time.time())),
                "category_id":          cat_map.get(ch["group"], "1"),
                "custom_sid":           "",
                "tv_archive":           0,
                "direct_source":        stream_url,
                "tv_archive_duration":  0
            })
        return Response(json.dumps(streams), mimetype="application/json")

    if action in ("get_short_epg", "get_simple_data_table"):
        return Response(json.dumps({"epg_listings": []}), mimetype="application/json")

    return Response(json.dumps([]), mimetype="application/json")


# XC-style stream URL: /<user>/<pass>/<stream_id>
@app.route("/<string:username>/<string:password>/<int:stream_id>")
def xc_stream(username: str, password: str, stream_id: int):
    if not xc_auth(username, password):
        return "Unauthorized", 401

    all_ch = get_all_channels_sorted()
    if stream_id < 1 or stream_id > len(all_ch):
        return "Not found", 404

    ch = all_ch[stream_id - 1]
    if ch.get("custom"):
        return redirect(ch["stream_url"])

    print(f"[XC] Token request: {ch['name']}")
    url = get_stream_url(ch["url"], ch["slug"])
    if not url:
        return "Stream unavailable", 503
    return redirect(url)


# Regular stream route
@app.route("/stream/<slug>")
def stream(slug: str):
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


@app.route("/debug")
def debug():
    info = {
        "channels_count": len(CHANNELS),
        "custom_count":   len(CUSTOM),
        "sample_channels": [
            {"name": c["name"], "slug": c["slug"], "group": c["group"]}
            for c in (CHANNELS + CUSTOM)[:10]
        ],
        "last_refresh": LAST_REFRESH,
        "xc_user": XC_USER,
    }
    return Response(json.dumps(info, indent=2), mimetype="application/json")


# ── 7. Entry point ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="TheTVApp Local Proxy + XC Server")
    ap.add_argument("--port",       type=int, default=8087)
    ap.add_argument("--custom",     default="")
    ap.add_argument("--refresh",    type=int, default=30)
    ap.add_argument("--epg",        default="")
    ap.add_argument("--jf-url",     default="")
    ap.add_argument("--jf-api-key", default="")
    ap.add_argument("--jf-task-id", default="")
    args = ap.parse_args()

    CUSTOM_FILE["path"] = args.custom
    EPG_URL["url"]      = args.epg
    JELLYFIN["url"]     = args.jf_url
    JELLYFIN["api_key"] = args.jf_api_key
    JELLYFIN["task_id"] = args.jf_task_id

    global PORT, REFRESH_EVERY, CHANNELS, CUSTOM
    PORT          = args.port
    REFRESH_EVERY = args.refresh

    print("[*] Fetching TVApp channel list ...")
    CHANNELS = fetch_channels()
    LAST_REFRESH["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LAST_REFRESH["next"] = f"in {REFRESH_EVERY} min"
    print(f"[+] {len(CHANNELS)} TVApp channels loaded.")

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
    print(f"[OK] Playlist URL   : http://localhost:{PORT}/playlist.m3u")
    print(f"[OK] XC API         : http://localhost:{PORT}/player_api.php")
    print(f"[OK] XC User        : {XC_USER}")
    print(f"[OK] XC Pass        : {XC_PASS}\n")

    start_refresh_timer()
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()