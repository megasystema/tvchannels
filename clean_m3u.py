#!/usr/bin/env python3
"""
clean_m3u.py
────────────
Lee un archivo M3U (por defecto my_channels.m3u),
testea cada canal (URL) y genera dos archivos:

- my_channels_working.m3u  → Sólo canales que responden
- my_channels_dead.m3u     → Canales que fallan

Uso:
    python clean_m3u.py               # usa my_channels.m3u
    python clean_m3u.py otra_lista.m3u
"""

import sys
import time
from pathlib import Path

import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

TIMEOUT = 5  # segundos por canal


def check_stream(url: str) -> bool:
    """
    Devuelve True si la URL responde con 200/206 dentro del TIMEOUT.
    No es perfecto (hay casos especiales) pero funciona muy bien en general.[web:166][web:170]
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            stream=True,
        )
        if resp.status_code in (200, 206):
            return True
        return False
    except Exception:
        return False


def split_channels(lines):
    """
    Separa la M3U en bloques de canales.
    Cada bloque = [#EXTINF..., posibles #EXTVLCOPT..., URL].
    """
    i = 0
    n = len(lines)
    header = []
    channels = []

    # Mantener el header inicial (#EXTM3U + posibles líneas extra)
    while i < n and lines[i].strip().startswith("#EXTM3U"):
        header.append(lines[i])
        i += 1

    # A partir de aquí, procesar canales
    while i < n:
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            block = [lines[i]]  # incluir la línea original con saltos
            i += 1
            # Incluir #EXTVLCOPT u otras directivas hasta URL
            url = None
            while i < n:
                l = lines[i].strip()
                if not l:
                    block.append(lines[i])
                    i += 1
                    continue
                if l.startswith("#EXT") and not l.startswith("#EXTINF"):
                    block.append(lines[i])
                    i += 1
                    continue
                # Si empieza por http/rtmp, asumimos que es la URL del canal
                if l.startswith("http") or l.startswith("rtmp"):
                    url = l
                    block.append(lines[i])
                    i += 1
                    break
                # Línea inesperada → se incluye y se sale
                block.append(lines[i])
                i += 1
                break

            if url:
                channels.append((block, url))
            else:
                # EXTINF sin URL clara → lo consideramos canal "muerto"
                channels.append((block, None))
        else:
            # Cualquier otra línea suelta
            header.append(lines[i])
            i += 1

    return header, channels


def main():
    # Nombre de archivo de entrada
    if len(sys.argv) > 1:
        input_name = sys.argv[1]
    else:
        input_name = "my_channels.m3u"

    path = Path(input_name)
    if not path.exists():
        print(f"[!] No se encontró el archivo: {input_name}")
        sys.exit(1)

    print(f"[*] Leyendo {input_name} ...")
    raw = path.read_text(encoding="utf-8", errors="ignore").splitlines(True)

    header, channels = split_channels(raw)
    total = len(channels)
    print(f"[*] Canales encontrados: {total}")

    working_blocks = []
    dead_blocks    = []

    for idx, (block, url) in enumerate(channels, start=1):
        # Obtener nombre legible
        name = "Desconocido"
        for line in block:
            if line.strip().startswith("#EXTINF"):
                parts = line.strip().split(",", 1)
                if len(parts) == 2:
                    name = parts[1].strip()
                break

        if not url:
            print(f"[{idx}/{total}] {name}: sin URL → DEAD")
            dead_blocks.extend(block)
            continue

        print(f"[{idx}/{total}] Probando: {name}")
        ok = check_stream(url)
        if ok:
            print(f"   -> OK")
            working_blocks.extend(block)
        else:
            print(f"   -> DEAD")
            dead_blocks.extend(block)

        # Pequeña pausa opcional para no saturar servidores
        time.sleep(0.1)

    # Escribir archivos resultantes
    base = path.stem  # "my_channels"
    work_file = path.with_name(f"{base}_working.m3u")
    dead_file = path.with_name(f"{base}_dead.m3u")

    print(f"\n[*] Escribiendo lista de canales activos en: {work_file.name}")
    work_file.write_text("".join(header + working_blocks), encoding="utf-8")

    print(f"[*] Escribiendo lista de canales muertos en: {dead_file.name}")
    dead_file.write_text("".join(header + dead_blocks), encoding="utf-8")

    print("\n[OK] Limpieza terminada.")
    print(f"    Activos: {len(working_blocks)} líneas (varias por canal)")
    print(f"    Muertos: {len(dead_blocks)} líneas (varias por canal)")


if __name__ == "__main__":
    main()