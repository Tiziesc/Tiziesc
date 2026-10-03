#!/usr/bin/env python3
"""Genera el gráfico de velas de las líneas netas escritas, en SVG claro y oscuro.

Modelo stock-flujo: el precio es K, las líneas netas acumuladas de código y de
documentación. Cada sesión (día con commits) abre en K, sube hasta K + agregadas,
baja hasta K - borradas y cierra en K + agregadas - borradas. El volumen es la
cantidad de commits de la sesión. Solo usa la biblioteca estándar de Python.

Uso:
  python scripts/ticker.py api   --login Tiziesc                 (necesita GH_PAT)
  python scripts/ticker.py local --repo RUTA --email MAIL [...]   (vista previa)
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- qué cuenta

CODE_EXT = {
    "py", "go", "ts", "tsx", "js", "jsx", "mjs", "cjs", "dart", "java", "kt", "kts",
    "swift", "c", "h", "cc", "cpp", "hpp", "cs", "rs", "rb", "php", "scala", "sql",
    "sh", "bash", "ps1", "lua", "r", "jl", "vue", "svelte", "css", "scss",
}
DOC_EXT = {"md", "mdx", "rst", "adoc"}
EXCLUDED_DIR = re.compile(
    r"(^|/)(vendor|node_modules|dist|build|third_party|\.venv|venv|__pycache__|generated)/"
)
EXCLUDED_FILE = re.compile(r"(\.min\.(js|css)|\.pb\.go|_pb2\.py|\.g\.dart|\.freezed\.dart|\.d\.ts)$")


def counts(path):
    """Código fuente o documentación escrita a mano; nada generado ni de terceros."""
    path = path.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if "." not in name or EXCLUDED_DIR.search(path) or EXCLUDED_FILE.search(path):
        return False
    return name.rsplit(".", 1)[-1].lower() in CODE_EXT | DOC_EXT


def renamed_target(path):
    """'src/{a => b}/x.py' o 'a.py => b.py' (numstat con renombres) -> ruta nueva."""
    path = re.sub(r"\{[^{}]*? => ([^{}]*)\}", r"\1", path)
    return path.split(" => ")[-1].replace("//", "/")


def commit_key(sha):
    # El ledger es público: se guarda un hash, nunca el SHA ni el nombre del repo.
    return hashlib.sha256(sha.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- fuentes de datos

def collect_local(repos, emails, tz, ledger):
    for repo in repos:
        cmd = ["git", "-C", repo, "log", "--all", "--no-merges", "--numstat", "--format=@%H %aI"]
        cmd += [f"--author={e}" for e in emails]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True).stdout
        key = None
        for line in out.splitlines():
            if line.startswith("@"):
                sha, stamp = line[1:].split(" ", 1)
                key = commit_key(sha)
                day = datetime.fromisoformat(stamp).astimezone(tz).date().isoformat()
                ledger[key] = {"d": day, "a": 0, "r": 0}
                continue
            parts = line.split("\t")
            if key and len(parts) == 3 and parts[0] != "-" and counts(renamed_target(parts[2])):
                ledger[key]["a"] += int(parts[0])
                ledger[key]["r"] += int(parts[1])


API = "https://api.github.com"


def gh(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "loc-ticker",
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp), resp.headers.get("Link", "")
        except urllib.error.HTTPError as err:
            if err.code in (404, 409):  # sin acceso o repositorio vacío
                return None, ""
            if err.code in (403, 429, 500, 502, 503) and attempt < 4:
                time.sleep(5 * 2 ** attempt)
                continue
            raise


def paginate(url, token):
    while url:
        data, link = gh(url, token)
        if not data:
            return
        yield from data
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = nxt.group(1) if nxt else None


def collect_api(login, token, tz, ledger, excluded):
    repos = paginate(f"{API}/user/repos?affiliation=owner,collaborator,organization_member&per_page=100", token)
    for repo in repos:
        full = repo["full_name"]
        if full.lower() in excluded or not repo.get("size"):
            continue
        new = 0
        for c in paginate(f"{API}/repos/{full}/commits?author={login}&per_page=100", token):
            key = commit_key(c["sha"])
            if len(c["parents"]) > 1 or key in ledger:
                continue
            adds = dels = 0
            page = 1
            while True:  # la API pagina los archivos de un commit de a 300
                detail, _ = gh(f"{API}/repos/{full}/commits/{c['sha']}?page={page}", token)
                files = (detail or {}).get("files", [])
                for f in files:
                    if counts(f["filename"]):
                        adds += f["additions"]
                        dels += f["deletions"]
                if len(files) < 300:
                    break
                page += 1
            stamp = datetime.fromisoformat(c["commit"]["author"]["date"].replace("Z", "+00:00"))
            ledger[key] = {"d": stamp.astimezone(tz).date().isoformat(), "a": adds, "r": dels}
            new += 1
        print(f"  {new:4d} commits nuevos", file=sys.stderr)


# ---------------------------------------------------------------- serie OHLC

def build_bars(ledger):
    days = {}
    for v in ledger.values():
        if v["a"] + v["r"] == 0:  # un commit que no toca código ni documentación no abre rueda
            continue
        d = days.setdefault(v["d"], [0, 0, 0])
        d[0] += v["a"]
        d[1] += v["r"]
        d[2] += 1
    k, bars = 0, []
    for day in sorted(days):
        adds, dels, n = days[day]
        bars.append({"t": date.fromisoformat(day), "o": k, "h": k + adds, "l": k - dels,
                     "c": k + adds - dels, "v": n})
        k = bars[-1]["c"]
    return bars


def to_weekly(bars):
    weeks = {}
    for b in bars:
        monday = b["t"] - timedelta(days=b["t"].weekday())
        w = weeks.get(monday)
        if w is None:
            weeks[monday] = dict(b, t=monday)
        else:
            w.update(h=max(w["h"], b["h"]), l=min(w["l"], b["l"]), c=b["c"], v=w["v"] + b["v"])
    return [weeks[k] for k in sorted(weeks)]


def sma(values, n):
    return [None if i + 1 < n else sum(values[i + 1 - n:i + 1]) / n for i in range(len(values))]


def patterns(bars):
    """Doji y velas envolventes con los umbrales del indicador de patrones de TradingView."""
    marks = {}
    for i, b in enumerate(bars):
        rng, body = b["h"] - b["l"], abs(b["c"] - b["o"])
        if rng > 0 and body <= 0.05 * rng:
            marks[i] = "D"
            continue
        if i == 0:
            continue
        p = bars[i - 1]
        pbody = abs(p["c"] - p["o"])
        if p["c"] < p["o"] and b["c"] > b["o"] and b["c"] >= p["o"] and b["o"] <= p["c"] and body > pbody:
            marks[i] = "BU"
        elif p["c"] > p["o"] and b["c"] < b["o"] and b["c"] <= p["o"] and b["o"] >= p["c"] and body > pbody:
            marks[i] = "BE"
    return marks


# ---------------------------------------------------------------- render SVG
#
# Tokens de UI_STANDARDS (Early Warning System): texto y divisor de §2.1, azul
# categórico de §2.4, escala de letra de §3 y espaciado de base 4 de §4. Las
# velas llevan el verde y el rojo pedidos; en el tema claro el verde baja un
# paso para medir 3:1 contra el fondo (AV-14), y el texto sobre un color de dato
# va en tinta oscura, que mide 5,8:1 o más (AV-46). Sin transparencias (AV-89):
# el volumen usa el tinte sólido de cada color sobre el fondo de GitHub.

THEMES = {
    "light": {"text": "#16212a", "muted": "#5b6b74", "divider": "#dce1e4",
              "up": "#41a352", "down": "#ff4c4c", "up_vol": "#b3daba", "down_vol": "#ffb7b7",
              "ma": "#2075c0", "neutral": "#5b6b74", "on_neutral": "#ffffff"},
    "dark": {"text": "#edf1f3", "muted": "#bfcbd1", "divider": "#26303a",
             "up": "#47b259", "down": "#ff4c4c", "up_vol": "#245131", "down_vol": "#6e292c",
             "ma": "#78aee6", "neutral": "#bfcbd1", "on_neutral": "#0b0f13"},
}
INK = "#0b0f13"
FONT = "'Segoe UI', Arial, Helvetica, 'Liberation Sans', sans-serif"
FS_BASE, FS_SMALL, FS_AUX = 16, 12.8, 10.24
MONTHS = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

W, H = 840, 420
AXIS_W = 64                       # escala de precios a la derecha
PANE_W = W - AXIS_W
PANE_B = H - 28                   # debajo, el eje temporal
PLOT_TOP = 92                     # las velas empiezan debajo de la leyenda
VOL_H = 0.16                      # el volumen ocupa el 16 % inferior del panel
MIN_SLOTS, RIGHT_OFFSET = 30, 2   # con pocas sesiones, las velas no se ensanchan de más


def num(x):
    return f"{round(x):,}".replace(",", ".")


def nice_step(span, target=6):
    raw = span / target
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return max(1, m * mag)
    return 10 * mag


def render(bars, avg, marks, theme, tf):
    t = THEMES[theme]
    n = len(bars)
    slots = max(n + RIGHT_OFFSET, MIN_SLOTS)
    step = PANE_W / slots
    body_w = max(1, min(round(step * 0.6), 14))

    def x(i):
        return round(PANE_W - (RIGHT_OFFSET + (n - 1 - i) + 0.5) * step)

    vol_top = PANE_B * (1 - VOL_H)
    plot_bot = vol_top - 16
    lo, hi = min(b["l"] for b in bars), max(b["h"] for b in bars)
    if hi == lo:
        hi += 1

    def y(p):
        return plot_bot - (p - lo) / (hi - lo) * (plot_bot - PLOT_TOP)

    def color(b):
        return t["up"] if b["c"] >= b["o"] else t["down"]

    last = bars[-1]
    y_last = y(last["c"])
    out = []
    add = out.append

    add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'font-family="{FONT}" role="img" aria-labelledby="ttl">')
    add(f'<title id="ttl">Líneas netas escritas, velas {"diarias" if tf == "1D" else "semanales"}: '
        f'cierre de {num(last["c"])} líneas el {last["t"]:%d/%m/%Y}</title>')
    add('<style>text{font-variant-numeric:tabular-nums}</style>')
    add(f'<defs><clipPath id="pane"><rect width="{PANE_W}" height="{PANE_B}"/></clipPath></defs>')

    # grilla horizontal y escala de precios
    p_top = lo + (hi - lo) * (plot_bot - 0) / (plot_bot - PLOT_TOP)
    p_bot = lo - (hi - lo) * (PANE_B - plot_bot) / (plot_bot - PLOT_TOP)
    tick = nice_step(p_top - p_bot)
    labels = []
    add(f'<g stroke="{t["divider"]}" shape-rendering="crispEdges">')
    p = (int(p_bot // tick) + 1) * tick
    while p < p_top:
        yy = round(y(p)) + 0.5
        if PLOT_TOP - 24 < yy < vol_top and (p >= 0 or p >= lo - tick / 2):
            add(f'<line x1="0" x2="{PANE_W}" y1="{yy}" y2="{yy}"/>')
            if abs(yy - y_last) > 16:
                labels.append(f'<text x="{PANE_W + 8}" y="{yy + 4}" font-size="{FS_SMALL}" '
                              f'fill="{t["muted"]}">{num(p)}</text>')
        p += tick
    add(f'<line x1="{PANE_W + 0.5}" x2="{PANE_W + 0.5}" y1="0" y2="{PANE_B}"/>')
    add(f'<line x1="0" x2="{W}" y1="{PANE_B + 0.5}" y2="{PANE_B + 0.5}"/>')
    add('</g>')

    # eje temporal: número de día, y el mes en semibold cuando cambia
    every = max(1, -(-56 // int(step)))
    prev_month = None
    for i in range(0, n, every):
        d = bars[i]["t"]
        month = (d.year, d.month)
        if prev_month is None or month != prev_month:
            label = str(d.year) if d.month == 1 and prev_month else MONTHS[d.month - 1]
            weight = 600
        else:
            label, weight = str(d.day), 400
        prev_month = month
        if x(i) < 16:  # una etiqueta cortada por el borde no se dibuja
            continue
        labels.append(f'<text x="{x(i)}" y="{PANE_B + 20}" font-size="{FS_SMALL}" font-weight="{weight}" '
                      f'text-anchor="middle" fill="{t["muted"]}">{label}</text>')

    add('<g clip-path="url(#pane)" shape-rendering="crispEdges">')
    # volumen, abajo del panel
    vmax = max(b["v"] for b in bars)
    for i, b in enumerate(bars):
        vh = max(1, round(b["v"] / vmax * (PANE_B - vol_top)))
        fill = t["up_vol"] if b["c"] >= b["o"] else t["down_vol"]
        add(f'<rect x="{x(i) - body_w // 2}" y="{PANE_B - vh}" width="{body_w}" height="{vh}" fill="{fill}"/>')

    # velas: la mecha de 1 px y el cuerpo, de al menos 1 px de alto
    for i, b in enumerate(bars):
        c, xx = color(b), x(i)
        add(f'<line x1="{xx + 0.5}" x2="{xx + 0.5}" y1="{round(y(b["h"]))}" y2="{round(y(b["l"]))}" stroke="{c}"/>')
        y0, y1 = sorted((round(y(b["o"])), round(y(b["c"]))))
        add(f'<rect x="{xx - body_w // 2}" y="{y0}" width="{body_w}" height="{max(1, y1 - y0)}" fill="{c}"/>')
    add('</g>')

    # media móvil de 9 sesiones
    pts = [f"{x(i) + 0.5},{y(v):.1f}" for i, v in enumerate(avg) if v is not None]
    if len(pts) > 1:
        add(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{t["ma"]}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>')

    # patrones: la etiqueta va arriba de la vela si es bajista y abajo si no
    for i, kind in marks.items():
        b, xx = bars[i], x(i) + 0.5
        fill = {"D": t["neutral"], "BU": t["up"], "BE": t["down"]}[kind]
        ink = t["on_neutral"] if kind == "D" else INK
        text = "D" if kind == "D" else "BE"
        w = 8 + 7 * len(text)
        if kind == "BE":
            tip = y(b["h"]) - 4
            add(f'<path d="M{xx},{tip:.1f} l-4,-5 h8 z" fill="{fill}"/>')
            box = tip - 5 - 16
        else:
            tip = y(b["l"]) + 4
            add(f'<path d="M{xx},{tip:.1f} l-4,5 h8 z" fill="{fill}"/>')
            box = tip + 5
        add(f'<rect x="{xx - w / 2}" y="{box:.1f}" width="{w}" height="16" rx="4" fill="{fill}"/>')
        add(f'<text x="{xx}" y="{box + 11.5:.1f}" font-size="{FS_AUX}" font-weight="700" '
            f'text-anchor="middle" fill="{ink}">{text}</text>')

    # línea y etiqueta del último precio
    last_c = color(last)
    yl = round(y_last) + 0.5
    add(f'<line x1="0" x2="{PANE_W}" y1="{yl}" y2="{yl}" stroke="{last_c}" stroke-dasharray="1 3"/>')
    out.extend(labels)
    add(f'<rect x="{PANE_W + 2}" y="{yl - 10}" width="{AXIS_W - 4}" height="20" rx="4" fill="{last_c}"/>')
    add(f'<text x="{PANE_W + 8}" y="{yl + 4.5}" font-size="{FS_SMALL}" font-weight="600" fill="{INK}">'
        f'{num(last["c"])}</text>')

    # leyenda: qué se mide, la última vela y la media; los valores en tinta, el color en la marca
    chg = last["c"] - last["o"]
    pct = f' ({chg / last["o"] * 100:+.2f} %)'.replace(".", ",") if last["o"] else ""
    m = t["muted"]
    period = "Diario" if tf == "1D" else "Semanal"
    add(f'<text x="0" y="20" font-size="{FS_BASE}" fill="{t["text"]}"><tspan font-weight="600">'
        f'Líneas netas escritas</tspan><tspan dx="12" font-size="{FS_SMALL}" fill="{m}">'
        f'{period} · al {last["t"]:%d/%m/%Y}</tspan></text>')
    tri = "M0,10 l5,-8 l5,8 z" if chg >= 0 else "M0,2 l5,8 l5,-8 z"
    add(f'<path d="{tri}" transform="translate(0 {32})" fill="{last_c}"/>')
    add(f'<text x="16" y="43" font-size="{FS_SMALL}" fill="{m}">'
        f'Cierre<tspan dx="4" fill="{t["text"]}" font-weight="500">{num(last["c"])}</tspan>'
        f'<tspan dx="4" fill="{t["text"]}">{"+" if chg >= 0 else "−"}{num(abs(chg))}{pct}</tspan>'
        f'<tspan dx="16">Apertura</tspan><tspan dx="4" fill="{t["text"]}">{num(last["o"])}</tspan>'
        f'<tspan dx="12">Máximo</tspan><tspan dx="4" fill="{t["text"]}">{num(last["h"])}</tspan>'
        f'<tspan dx="12">Mínimo</tspan><tspan dx="4" fill="{t["text"]}">{num(last["l"])}</tspan>'
        f'<tspan dx="12">Volumen</tspan><tspan dx="4" fill="{t["text"]}">'
        f'{last["v"]} {"commit" if last["v"] == 1 else "commits"}</tspan></text>')
    ma_last = next((a for a in reversed(avg) if a is not None), None)
    if ma_last is not None:
        add(f'<line x1="0" x2="10" y1="58" y2="58" stroke="{t["ma"]}" stroke-width="2" stroke-linecap="round"/>')
        add(f'<text x="16" y="62" font-size="{FS_SMALL}" fill="{m}">Media móvil de 9 '
            f'{"sesiones" if tf == "1D" else "semanas"}<tspan dx="4" fill="{t["text"]}">{num(ma_last)}</tspan></text>')
    add('</svg>')
    return "\n".join(out)


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="source", required=True)
    api = sub.add_parser("api", help="lee todos tus repos con la API de GitHub (GH_PAT)")
    api.add_argument("--login", required=True)
    api.add_argument("--exclude", nargs="*", default=[], help="repos owner/nombre a ignorar")
    loc = sub.add_parser("local", help="lee clones locales (vista previa)")
    loc.add_argument("--repo", nargs="+", required=True)
    loc.add_argument("--email", nargs="+", required=True)
    for p in (api, loc):
        p.add_argument("--ledger", help="caché JSON de commits ya procesados")
        p.add_argument("--out", default=str(ROOT / "charts"))
        p.add_argument("--timeframe", choices=["auto", "1D", "1W"], default="auto")
        p.add_argument("--utc-offset", type=float, default=-3, help="zona horaria del día de cada commit")
    args = ap.parse_args()

    tz = timezone(timedelta(hours=args.utc_offset))
    ledger_path = Path(args.ledger) if args.ledger else (ROOT / "data" / "ledger.json" if args.source == "api" else None)
    ledger = json.loads(ledger_path.read_text()) if ledger_path and ledger_path.exists() else {}

    if args.source == "api":
        token = os.environ.get("GH_PAT")
        if not token:
            sys.exit("Falta la variable de entorno GH_PAT")
        excluded = {e.lower() for e in args.exclude} | {f"{args.login}/{args.login}".lower()}
        collect_api(args.login, token, tz, ledger, excluded)
    else:
        collect_local(args.repo, args.email, tz, ledger)

    if ledger_path:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(dict(sorted(ledger.items())), indent=0) + "\n")

    bars = build_bars(ledger)
    if not bars:
        sys.exit("No hay commits de código ni de documentación para graficar")
    tf = args.timeframe if args.timeframe != "auto" else ("1D" if len(bars) <= 90 else "1W")
    if tf == "1W":
        bars = to_weekly(bars)
    window = 52 if tf == "1W" else 60
    avg = sma([b["c"] for b in bars], 9)
    marks = patterns(bars)
    start = max(0, len(bars) - window)
    shown, shown_avg = bars[start:], avg[start:]
    shown_marks = {i - start: k for i, k in marks.items() if i >= start}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        (out / f"ticker-{theme}.svg").write_text(render(shown, shown_avg, shown_marks, theme, tf), encoding="utf-8")
    print(f"{len(shown)} velas {tf}, cierre {num(bars[-1]['c'])}, patrones {shown_marks}", file=sys.stderr)


if __name__ == "__main__":
    main()
