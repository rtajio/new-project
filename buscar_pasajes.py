#!/usr/bin/env python3
"""
Buscador y monitor de pasajes aéreos (por defecto: Tacna TCQ -> Lima LIM).

Consulta varias fuentes simulando distintos dispositivos y mercados
(escritorio/móvil, Perú/EE. UU./Chile), convierte todo a soles, guarda
cada observación en SQLite y muestra un ranking con el precio más bajo.

Uso rápido:
    python buscar_pasajes.py                      # 6-nov-2026, todas las fuentes
    python buscar_pasajes.py --flex 2             # también 4, 5, 7 y 8 de noviembre
    python buscar_pasajes.py --cada 180 --rondas 8   # monitorear cada 3 h
    python buscar_pasajes.py --historial          # estadísticas acumuladas
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import statistics
import sys
import urllib.request
from dataclasses import dataclass, asdict

from playwright.async_api import async_playwright, Browser

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "precios.db")
CHROMIUM = os.environ.get("CHROMIUM_PATH") or (
    "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
)
AEROLINEAS = re.compile(r"^(LATAM|Sky Airline|SKY|JetSMART|Avianca|Aerom[eé]xico|Star Per[uú]|ATSA)", re.I)
HORA = re.compile(r"^\d{1,2}:\d{2}(\s?[ap]\.?\s?m\.?)?$", re.I)
PRECIO = re.compile(r"^(PEN|S/|US\$|USD|CLP|CLP\$|\$)\s?([\d.,]+)$")


@dataclass
class Tarifa:
    fuente: str
    perfil: str
    fecha_vuelo: str
    aerolinea: str
    salida: str
    llegada: str
    moneda: str
    precio: float
    precio_pen: float
    url: str
    nota: str = ""
    capturado: str = ""


# ---------------------------------------------------------------- utilidades
def tipo_cambio() -> dict[str, float]:
    """Soles por unidad de cada moneda (USD, CLP). Cae a valores fijos si no hay red."""
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/PEN", timeout=10) as r:
            rates = json.load(r)["rates"]
        return {"PEN": 1.0, "USD": 1 / rates["USD"], "CLP": 1 / rates["CLP"]}
    except Exception:
        return {"PEN": 1.0, "USD": 3.47, "CLP": 0.0037}


def numero(txt: str) -> float:
    """Convierte '1.234,56', '1,234.56' o '226,12' a float."""
    txt = txt.strip()
    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".") if txt.rfind(",") > txt.rfind(".") else txt.replace(",", "")
    elif "," in txt:
        ent, dec = txt.rsplit(",", 1)
        txt = f"{ent}.{dec}" if len(dec) == 2 else txt.replace(",", "")
    elif txt.count(".") == 1 and len(txt.split(".")[1]) == 3:
        txt = txt.replace(".", "")  # 140.000 CLP
    return float(txt)


def moneda_de(simbolo: str, defecto: str) -> str:
    return {"PEN": "PEN", "S/": "PEN", "US$": "USD", "USD": "USD", "CLP": "CLP", "CLP$": "CLP"}.get(simbolo, defecto)


# ---------------------------------------------------------------- perfiles
# Cada perfil imita una forma distinta de entrar: dispositivo + idioma + país + moneda.
PERFILES_GOOGLE = [
    {"nombre": "PC Perú (PEN)", "device": None, "hl": "es-419", "gl": "pe", "curr": "PEN"},
    {"nombre": "Móvil Perú (PEN)", "device": "iPhone 13", "hl": "es-419", "gl": "pe", "curr": "PEN"},
    {"nombre": "PC EE. UU. (USD)", "device": None, "hl": "en", "gl": "us", "curr": "USD"},
    {"nombre": "Android Chile (CLP)", "device": "Pixel 7", "hl": "es-419", "gl": "cl", "curr": "CLP"},
]
PERFILES_SKY = [
    {"nombre": "PC sitio Perú", "device": None, "mercado": "peru"},
    {"nombre": "Móvil sitio Perú", "device": "Pixel 7", "mercado": "peru"},
]


async def nueva_pagina(browser: Browser, pw, device: str | None):
    opts = dict(pw.devices[device]) if device else {"viewport": {"width": 1366, "height": 900}}
    ctx = await browser.new_context(locale="es-PE", timezone_id="America/Lima", **opts)
    return ctx, await ctx.new_page()


# ---------------------------------------------------------------- Google Flights
def parsear_google(texto: str) -> list[tuple[str, str, str, str | None, float | None]]:
    """Devuelve (aerolínea, salida, llegada, símbolo, precio) leyendo el texto visible.
    Funciona con el diseño de escritorio y el de móvil (el orden de los campos cambia)."""
    lineas = [l.strip() for l in texto.splitlines() if l.strip() and l.strip() != "–"]
    inicios = [i for i, l in enumerate(lineas) if HORA.match(l)]
    tarjetas, i = [], 0
    while i < len(inicios):
        a = inicios[i]
        b = inicios[i + 2] if i + 2 < len(inicios) else min(a + 14, len(lineas))
        bloque = lineas[a:b]
        horas = [l for l in bloque if HORA.match(l)][:2]
        aero = next((AEROLINEAS.match(l).group(1) for l in bloque if AEROLINEAS.match(l)), None)
        precio = next((PRECIO.match(l) for l in bloque if PRECIO.match(l)), None)
        if len(horas) == 2 and aero:
            sim, val = (precio.group(1), numero(precio.group(2))) if precio else (None, None)
            tarjetas.append((aero, horas[0], horas[1], sim, val))
            i += 2
        else:
            i += 1
    vistos, unicos = set(), []
    for t in tarjetas:  # Google repite vuelos entre "principales" y "otros"
        if t[:3] not in vistos:
            vistos.add(t[:3]); unicos.append(t)
    return unicos


async def google_flights(browser, pw, origen, destino, fecha, perfil, fx) -> list[Tarifa]:
    url = (f"https://www.google.com/travel/flights?q=Flights%20to%20{destino}%20from%20{origen}"
           f"%20on%20{fecha}%20oneway&curr={perfil['curr']}&hl={perfil['hl']}&gl={perfil['gl']}")
    ctx, pg = await nueva_pagina(browser, pw, perfil["device"])
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(9000)
        texto = await pg.inner_text("body")
    finally:
        await ctx.close()
    out = []
    for aero, sal, lleg, sim, val in parsear_google(texto):
        if val is None:  # Google a veces no muestra precio de SKY: se cubre con su propio scraper
            continue
        mon = perfil["curr"] if sim == "$" else moneda_de(sim, perfil["curr"])
        out.append(Tarifa("Google Flights", perfil["nombre"], fecha, aero, sal, lleg, mon, val,
                          round(val * fx[mon], 2), url))
    return out


# ---------------------------------------------------------------- SKY Airline (sitio oficial)
SKY_VUELO = re.compile(
    r"(?P<nota>TARIFA [A-ZÁÉÍÓÚ ]+)?\|?(?:QUEDAN (?P<asientos>\d+) ASIENTOS[^|]*)?\|?"
    r"(?P<sal>\d\d:\d\d)\|[A-Z]{3}\|[^|]*\|[^|]*\|(?P<lleg>\d\d:\d\d)\|[A-Z]{3}\|.*?"
    r"USD\s*(?P<usd>[\d.,]+)\|\|\s*S/\s*(?P<pen>[\d.,]+)")
SKY_CAL = re.compile(r"(?:LUN|MAR|MIÉ|JUE|VIE|SÁB|DOM), (\d{1,2}) ([A-Z]{3})\|\|USD\s*([\d.,]+)")
MESES = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8,
         "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12}


async def sky(browser, pw, origen, destino, fecha, perfil, fx) -> tuple[list[Tarifa], dict[str, float]]:
    url = (f"https://initial-sale.skyairline.com/es/{perfil['mercado']}?origin={origen}&destination={destino}"
           f"&departureDate={fecha}&arrivalDate={fecha}&flightType=OW&ADT=1")
    ctx, pg = await nueva_pagina(browser, pw, perfil["device"])
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await pg.wait_for_selector("text=Elegir vuelo", timeout=40000)
        except Exception:
            pass
        await pg.wait_for_timeout(2000)
        texto = "|".join(l.strip() for l in (await pg.inner_text("body")).replace("\xa0", " ").splitlines())
    finally:
        await ctx.close()
    out = []
    for m in SKY_VUELO.finditer(texto):
        usd = numero(m["usd"])
        nota = " ".join(x for x in [(m["nota"] or "").title(), f"quedan {m['asientos']} asientos" if m["asientos"] else "",
                                         f"SKY muestra S/ {m['pen']}"] if x)
        out.append(Tarifa("SKY (oficial)", perfil["nombre"], fecha, "Sky Airline", m["sal"], m["lleg"],
                          "USD", usd, round(usd * fx["USD"], 2), url, nota))
    # Calendario de tarifas más bajas que SKY muestra alrededor de la fecha
    anio = int(fecha[:4])
    cal = {}
    for d, mes, usd in SKY_CAL.findall(texto):
        if mes in MESES:
            cal[dt.date(anio, MESES[mes], int(d)).isoformat()] = numero(usd)
    return out, cal


# ---------------------------------------------------------------- persistencia
def guardar(tarifas: list[Tarifa]):
    con = sqlite3.connect(DB_PATH)
    campos = list(Tarifa.__dataclass_fields__)
    con.execute(f"CREATE TABLE IF NOT EXISTS tarifas ({', '.join(campos)})")
    con.executemany(f"INSERT INTO tarifas VALUES ({', '.join('?' * len(campos))})",
                    [tuple(asdict(t).values()) for t in tarifas])
    con.commit(); con.close()


def exportar_csv(ruta: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT * FROM tarifas ORDER BY capturado")
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow([d[0] for d in cur.description]); w.writerows(cur)
    con.close()


def historial(fecha: str):
    if not os.path.exists(DB_PATH):
        print("Aún no hay datos. Ejecuta una búsqueda primero."); return
    con = sqlite3.connect(DB_PATH)
    filas = con.execute("SELECT fuente, perfil, aerolinea, precio_pen, capturado FROM tarifas "
                        "WHERE fecha_vuelo=? ORDER BY capturado", (fecha,)).fetchall()
    con.close()
    if not filas:
        print(f"Sin observaciones para {fecha}."); return
    grupos: dict[tuple, list[float]] = {}
    for f, p, a, pen, _ in filas:
        grupos.setdefault((f, p, a), []).append(pen)
    print(f"\nHistorial {fecha}: {len(filas)} observaciones, "
          f"{len({r[4][:16] for r in filas})} capturas ({filas[0][4][:16]} → {filas[-1][4][:16]})\n")
    print(f"{'Fuente / perfil / aerolínea':58} {'n':>3} {'mín':>8} {'mediana':>8} {'máx':>8} {'DE':>6}")
    for (f, p, a), v in sorted(grupos.items(), key=lambda kv: min(kv[1])):
        de = statistics.stdev(v) if len(v) > 1 else 0.0
        print(f"{f + ' / ' + p + ' / ' + a:58} {len(v):>3} {min(v):>8.2f} {statistics.median(v):>8.2f} {max(v):>8.2f} {de:>6.2f}")
    mejor = min(filas, key=lambda r: r[3])
    print(f"\nMínimo histórico: S/ {mejor[3]:.2f} ({mejor[2]}, {mejor[0]} – {mejor[1]}, visto {mejor[4][:16]})")


# ---------------------------------------------------------------- orquestación
async def una_ronda(args) -> list[Tarifa]:
    fx = tipo_cambio()
    base = dt.date.fromisoformat(args.fecha)
    fechas = [(base + dt.timedelta(days=d)).isoformat() for d in range(-args.flex, args.flex + 1)]
    ahora = dt.datetime.now().isoformat(timespec="seconds")
    tareas, etiquetas = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROMIUM, headless=not args.ver,
                                           args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        sem = asyncio.Semaphore(args.paralelo)

        async def limitado(coro):
            async with sem:
                return await coro

        for f in fechas:
            if "google" in args.fuentes:
                for p in PERFILES_GOOGLE:
                    tareas.append(limitado(google_flights(browser, pw, args.origen, args.destino, f, p, fx)))
                    etiquetas.append(f"Google · {p['nombre']} · {f}")
            if "sky" in args.fuentes:
                for p in PERFILES_SKY:
                    tareas.append(limitado(sky(browser, pw, args.origen, args.destino, f, p, fx)))
                    etiquetas.append(f"SKY · {p['nombre']} · {f}")
        resultados = await asyncio.gather(*tareas, return_exceptions=True)
        await browser.close()

    tarifas, calendario = [], {}
    for etq, r in zip(etiquetas, resultados):
        if isinstance(r, Exception):
            print(f"  ! {etq}: {type(r).__name__}: {str(r)[:120]}", file=sys.stderr); continue
        if isinstance(r, tuple):
            r, cal = r
            for k, v in cal.items():
                calendario[k] = min(v, calendario.get(k, v))
        print(f"  ✓ {etq}: {len(r)} tarifas", file=sys.stderr)
        tarifas.extend(r)
    for t in tarifas:
        t.capturado = ahora
    guardar(tarifas)
    mostrar(tarifas, calendario, fx, args)
    return tarifas


def mostrar(tarifas: list[Tarifa], calendario: dict[str, float], fx, args):
    if not tarifas:
        print("No se obtuvieron tarifas (¿bloqueo anti-bot o sin red?)."); return
    tarifas.sort(key=lambda t: t.precio_pen)
    print(f"\n{args.origen} → {args.destino}  |  TC: 1 USD = S/ {fx['USD']:.3f}  |  {tarifas[0].capturado}\n")
    print(f"{'S/':>8}  {'original':>14}  {'fecha':10}  {'salida':>10}  {'aerolínea':12}  {'fuente / perfil':38} nota")
    for t in tarifas[: args.top]:
        print(f"{t.precio_pen:>8.2f}  {t.moneda + ' ' + format(t.precio, ',.2f'):>14}  {t.fecha_vuelo:10}  "
              f"{t.salida:>10}  {t.aerolinea[:12]:12}  {(t.fuente + ' / ' + t.perfil)[:38]:38} {t.nota}")
    # ¿El precio cambia según dispositivo o mercado? Comparamos el mismo vuelo entre perfiles.
    por_vuelo: dict[tuple, list[Tarifa]] = {}
    for t in tarifas:
        por_vuelo.setdefault((t.fecha_vuelo, t.aerolinea, t.salida.replace(" ", "").lower()), []).append(t)
    difs = [(k, v) for k, v in por_vuelo.items() if len(v) > 1 and max(x.precio_pen for x in v) - min(x.precio_pen for x in v) >= 3]
    if difs:
        print("\nMismo vuelo, distinto precio según perfil (≥ S/ 3):")
        for (f, a, s), v in sorted(difs, key=lambda kv: min(x.precio_pen for x in kv[1])):
            v.sort(key=lambda x: x.precio_pen)
            print(f"  {f} {a} {s}: S/ {v[0].precio_pen:.2f} ({v[0].perfil}) vs S/ {v[-1].precio_pen:.2f} ({v[-1].perfil})")
    if calendario:
        print("\nCalendario SKY (tarifa más baja por día, USD → S/):")
        for d in sorted(calendario):
            marca = " ◀" if d == args.fecha else ""
            print(f"  {d}  USD {calendario[d]:>7.2f}  ≈ S/ {calendario[d] * fx['USD']:>7.2f}{marca}")
    m = tarifas[0]
    print(f"\n★ Mejor precio: S/ {m.precio_pen:.2f} — {m.aerolinea} {m.fecha_vuelo} {m.salida} "
          f"({m.fuente}, {m.perfil})\n  {m.url}")


def main():
    ap = argparse.ArgumentParser(description="Busca el pasaje más barato entre dos aeropuertos.")
    ap.add_argument("--origen", default="TCQ")
    ap.add_argument("--destino", default="LIM")
    ap.add_argument("--fecha", default="2026-11-06", help="AAAA-MM-DD")
    ap.add_argument("--flex", type=int, default=0, help="días de flexibilidad ± alrededor de la fecha")
    ap.add_argument("--fuentes", default="google,sky", help="lista separada por comas: google,sky")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--paralelo", type=int, default=3, help="navegadores simultáneos")
    ap.add_argument("--cada", type=int, default=0, help="minutos entre rondas (0 = una sola vez)")
    ap.add_argument("--rondas", type=int, default=1)
    ap.add_argument("--historial", action="store_true", help="solo mostrar estadísticas guardadas")
    ap.add_argument("--csv", help="exportar todas las observaciones a este CSV")
    ap.add_argument("--ver", action="store_true", help="abrir el navegador visible (útil en tu PC)")
    args = ap.parse_args()
    args.fuentes = {f.strip().lower() for f in args.fuentes.split(",")}

    if args.historial:
        historial(args.fecha)
    else:
        for n in range(args.rondas if args.cada else 1):
            if n:
                print(f"\nEsperando {args.cada} min para la ronda {n + 1}/{args.rondas}…", file=sys.stderr)
                asyncio.run(asyncio.sleep(args.cada * 60))
            asyncio.run(una_ronda(args))
        if args.cada:
            historial(args.fecha)
    if args.csv:
        exportar_csv(args.csv); print(f"CSV exportado en {args.csv}")


if __name__ == "__main__":
    main()
