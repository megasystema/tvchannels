#!/usr/bin/env python3
"""
TheTVApp Local/Remote Proxy Server
──────────────────────────────────
- Une canales de TheTVApp + tu playlist custom (my_channels.m3u)
- Agrupa TheTVApp como:
    1 TVApp Entretenimiento
    1 TVApp Deportes
- Agrupa canales custom solo por país:
    Dominicana, Colombia, Puerto Rico, USA, etc.
- Expone playlist M3U en: /playlist.m3u

Uso típico (local o Railway):

    python thetvapp_proxy.py --custom my_channels.m3u \
        --epg "https://epg.pw/api/epg.xml?lang=en"
"""

import os
import re
import time
import threading
import asyncio
from datetime import datetime
from pathlib import Path
from collections import Counter

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect

# ───────────────────────────────────────────────────────────────────────────────
# Configuración básica
# ───────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://thetvapp.to/",
}

BASE_URL      = "https://thetvapp.to"
PORT          = int(os.environ.get("PORT", "8087"))  # Compatible con Railway
CACHE_TTL     = 300      # 5 minutos por token de stream
REFRESH_EVERY = 30       # minutos para refrescar la lista de canales TVApp

app      = Flask(__name__)
CHANNELS = []            # Canales de TheTVApp
CUSTOM   = []            # Canales de my_channels.m3u
TOKEN_CACHE: dict[str, tuple[str, float]] = {}

LAST_REFRESH = {"time": None, "next": None}
CUSTOM_FILE  = {"path": ""}
EPG_URL      = {"url": ""}

# ───────────────────────────────────────────────────────────────────────────────
# Mapa de países (tvg-id -> nombre de país en español)
# ───────────────────────────────────────────────────────────────────────────────

COUNTRY_MAP = {
    "do": "Dominicana",
    "pr": "Puerto Rico",
    "co": "Colombia",
    "ve": "Venezuela",
    "mx": "México",
    "ar": "Argentina",
    "cl": "Chile",
    "pe": "Perú",
    "ec": "Ecuador",
    "bo": "Bolivia",
    "py": "Paraguay",
    "uy": "Uruguay",
    "gt": "Guatemala",
    "hn": "Honduras",
    "sv": "El Salvador",
    "ni": "Nicaragua",
    "cr": "Costa Rica",
    "pa": "Panamá",
    "cu": "Cuba",
    "ht": "Haití",
    "br": "Brasil",
    "us": "USA",
    "ca": "Canadá",
    "es": "España",
    "pt": "Portugal",
    "fr": "Francia",
    "it": "Italia",
    "de": "Alemania",
    "gb": "Reino Unido",
    "nl": "Países Bajos",
    "za": "Sudáfrica",
    "ng": "Nigeria",
    "gh": "Ghana",
    "au": "Australia",
    "at": "Austria",
}

def extract_country_from_tvgid(tvg_id: str) -> str:
    """
    Extrae el país a partir de tvg-id como:
    - Telemundo.us
    - CanalX.do@SD
    """
    m = re.search(r"\.([a-z]{2})(?:@|$)", tvg_id.lower())
    if m:
        code = m.group(1)
        return COUNTRY_MAP.get(code, code.upper())
    return "Otros"


# ───────────────────────────────────────────────────────────────────────────────
# 1. Scrape de canales TheTVApp con grupos "1 TVApp ..."
# ───────────────────────────────────────────────────────────────────────────────

def fetch_channels() -> list[dict]:
    """Obtiene la lista de canales de TheTVApp."""
    resp = requests.get(BASE_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    entries, seen = [], set()

    # Canales regulares (entretenimiento / live TV)
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
            # Prefijo "1" para que salgan primero en los players
            "group": "1 TVApp Entretenimiento",
            "logo": "",
            "tvg_id": "",
            "custom": False,
        })

    # Eventos / deportes
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
            "group": "1 TVApp Deportes",
            "logo": "",
            "tvg_id": "",
            "custom": False,
        })

    return entries


def refresh_channels():
    """Refresca periódicamente la lista de canales de TheTVApp."""
    global CHANNELS
    try:
        print(f"\n[↻] Refreshing TVApp at {datetime.now().strftime('%H:%M:%S')} ...")
        new_channels = fetch_channels()
        new_slugs = {ch["slug"] for ch in new_channels}
        # Limpia tokens de canales que ya no existen
        for s in [s for s in list(TOKEN_CACHE.keys()) if s not in new_slugs]:
            del TOKEN_CACHE[s]
        CHANNELS = new_channels
        LAST_REFRESH["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LAST_REFRESH["next"] = f"in {REFRESH_EVERY} min"
        print(f"[+] {len(CHANNELS)} TVApp channels. Next refresh {LAST_REFRESH['next']}.")
    except Exception as e:
        print(f"[!] Refresh failed: {e}")


def start_refresh_timer():
    """Hilo en background que refresca cada REFRESH_EVERY minutos."""
    def loop():
        while True:
            time.sleep(REFRESH_EVERY * 60)
            refresh_channels()
    threading.Thread(target=loop, daemon=True).start()


# ───────────────────────────────────────────────────────────────────────────────
# 2. Parse de m3u custom (grupos = sólo país)
# ───────────────────────────────────────────────────────────────────────────────

def parse_custom_m3u(filepath: str) -> list[dict]:
    """Lee my_channels.m3u y agrupa canales solo por país."""
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

            # País a partir de tvg-id
            country = extract_country_from_tvgid(tvg_id) if tvg_id else "Otros"

            # Saltar líneas #EXTXXXX hasta encontrar la URL
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
                    "group": country,   # <── SOLO PAÍS
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


# ───────────────────────────────────────────────────────────────────────────────
# 3. Playwright para obtener tokens .m3u8 de TheTVApp
# ───────────────────────────────────────────────────────────────────────────────

async def _get_stream_url(page_url: str) -> str | None:
    """Usa Playwright + Chromium para capturar el primer .m3u8 de la página."""
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
    """Cachea cada token de stream durante CACHE_TTL segundos."""
    cached_url, cached_at = TOKEN_CACHE.get(slug, (None, 0))
    if cached_url and (time.time() - cached_at) < CACHE_TTL:
        return cached_url
    url = asyncio.run(_get_stream_url(page_url))
    if url:
        TOKEN_CACHE[slug] = (url, time.time())
    return url


# ───────────────────────────────────────────────────────────────────────────────
# 4. HTML simple (opcional para ver lista en navegador)
# ───────────────────────────────────────────────────────────────────────────────

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
    input{background:#1a1a2e;border:1px solid #0e4d6e;color:#eee;padding:.45rem .9rem;border-radius:6px;font-size:.9rem;width:260px;margin-bottom:1rem}
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
  <h1>&#128250; TheTVApp Proxy</h1>
  <div class="info">
    Playlist URL (TiviMate / IPTV Smarters):
    <code>http://localhost:PORT_PLACEHOLDER/playlist.m3u</code>
  </div>
  <div class="meta">
    <span>Total: <b>COUNT_PLACEHOLDER</b></span>
    <span>TVApp: <b>TVAPP_COUNT</b></span>
    <span>Custom: <b>CUSTOM_COUNT</b></span>
    <span>Last refresh: <b>LAST_REFRESH</b></span>
    <span>Next: <b>NEXT_REFRESH</b></span>
    <a href="/refresh">&#8635; Refresh Now</a>
  </div>
  <input type="text" id="search" placeholder="Buscar canal..." oninput="applyFilters()">
  <table id="tbl">
    <thead>
      <tr><th>#</th><th>Logo</th><th>Canal</th><th>Grupo</th><th>Play</th><th>URL</th></tr>
    </thead>
    <tbody>
ROWS_PLACEHOLDER
    </tbody>
  </table>
  <script>
    function applyFilters(){
      var q  = document.getElementById('search').value.toLowerCase();
      document.querySelectorAll('#tbl tbody tr').forEach(function(r){
        var txt  = r.textContent.toLowerCase();
        r.style.display = (!q || txt.includes(q)) ? '' : 'none';
      });
    }
  </script>
</body>
</html>"""


# ───────────────────────────────────────────────────────────────────────────────
# 5. Rutas Flask
# ───────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    all_channels = CHANNELS + CUSTOM
    sorted_channels = sorted(all_channels, key=lambda x: (x["group"], x["name"]))

    rows = ""
    for i, ch in enumerate(sorted_channels, 1):
        stream_link = ch["stream_url"] if ch.get("custom") else f"http://localhost:{PORT}/stream/{ch['slug']}"
        logo        = ch.get("logo", "")
        logo_html   = f'<img class="logo" src="{logo}" alt="" loading="lazy">' if logo else ""
        group       = ch.get("group", "")

        rows += (
            f'<tr>'
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
        .replace("PORT_PLACEHOLDER",  str(PORT))
        .replace("COUNT_PLACEHOLDER", str(len(all_channels)))
        .replace("TVAPP_COUNT",       str(len(CHANNELS)))
        .replace("CUSTOM_COUNT",      str(len(CUSTOM)))
        .replace("LAST_REFRESH",      LAST_REFRESH["time"] or "N/A")
        .replace("NEXT_REFRESH",      LAST_REFRESH["next"] or "N/A")
        .replace("REFRESH_SEC",       str(REFRESH_EVERY * 60))
        .replace("ROWS_PLACEHOLDER",  rows)
    )
    return Response(html, mimetype="text/html")


@app.route("/playlist.m3u")
def playlist():
    all_channels = sorted(CHANNELS + CUSTOM, key=lambda x: (x["group"], x["name"]))
    epg          = EPG_URL["url"]

    header = "#EXTM3U"
    if epg:
        header += f' url-tvg="{epg}" tvg-shift="-5"'
    lines = [header]

    # Asignar números de canal por grupo (bloques de 100)
    group_order = []
    for ch in all_channels:
        grp = ch.get("group", "Otros")
        if grp not in group_order:
            group_order.append(grp)

    group_base = {grp: (i + 1) * 100 for i, grp in enumerate(group_order)}
    counters   = {grp: 0 for grp in group_order}

    for ch in all_channels:
        group     = ch.get("group", "Otros")
        base      = group_base[group]
        counters[group] += 1
        chno      = base + counters[group]

        stream_url = ch["stream_url"] if ch.get("custom") else f"http://localhost:{PORT}/stream/{ch['slug']}"
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


# ───────────────────────────────────────────────────────────────────────────────
# 6. main()
# ───────────────────────────────────────────────────────────────────────────────

def main():
    import argparse

    ap = argparse.ArgumentParser(description="TheTVApp Local/Remote Proxy Server")
    ap.add_argument("--custom",  default="", help="Ruta al archivo .m3u custom (ej. my_channels.m3u)")
    ap.add_argument("--refresh", type=int, default=30, help="Minutos entre refrescos de canales TVApp")
    ap.add_argument("--epg",     default="", help="URL XMLTV de EPG (opcional)")
    args = ap.parse_args()

    CUSTOM_FILE["path"] = args.custom
    EPG_URL["url"]      = args.epg

    global REFRESH_EVERY, CHANNELS, CUSTOM
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

    print(f"\n[OK] Total channels : {len(CHANNELS) + len(CUSTOM)}")
    print(f"[OK] Auto-refresh   : every {REFRESH_EVERY} minutes")
    print(f"[OK] Browser UI     : http://localhost:{PORT}/")
    print(f"[OK] Playlist URL   : http://localhost:{PORT}/playlist.m3u\n")

    start_refresh_timer()
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()