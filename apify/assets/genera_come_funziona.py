#!/usr/bin/env python3
"""Il diagramma «come funziona» per i README Apify — v2, su misura per
ogni Actor (06/10/2026). Tre stazioni con icone, il mini-feed di offerte
vere della nicchia in mezzo (NEW/UPDATED/CLOSED come nella landing),
frecce gradiente con alone. Rende 4 varianti in vetrina/_come_*.html.

    python apify/assets/genera_come_funziona.py
"""

BASE_HEAD = """<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800;900&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>body{margin:0}</style></head><body>"""

FRECCIA = """<svg width="86" height="64" viewBox="0 0 86 64" style="flex-shrink:0">
<defs><linearGradient id="f{id}" x1="0" x2="1"><stop offset="0" stop-color="#5e8bff"/><stop offset="1" stop-color="#2fe0b6"/></linearGradient>
<filter id="glow{id}"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
<g filter="url(#glow{id})"><path d="M6 32 H64" stroke="url(#f{id})" stroke-width="5" stroke-linecap="round"/>
<path d="M52 14 L76 32 L52 50" fill="none" stroke="url(#f{id})" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></g></svg>"""

ICONA = {
    "filtro": '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5h18l-7 8v5l-4 2v-7z"/></svg>',
    "indice": '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>',
    "download": '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>',
}

BADGE = {"NEW": ("rgba(47,224,182,.18)", "#2fe0b6"),
         "UPDATED": ("rgba(94,139,255,.18)", "#8ab8ff"),
         "CLOSED": ("rgba(255,255,255,.10)", "rgba(255,255,255,.45)")}


def card(num, icona, titolo, corpo, extra=""):
    return f"""
    <div style="width:400px;border-radius:22px;border:1px solid rgba(255,255,255,.12);background:linear-gradient(160deg,rgba(255,255,255,.08),rgba(255,255,255,.02));padding:28px 30px;position:relative;overflow:hidden">
      <span style="position:absolute;top:-14px;right:8px;font-size:86px;font-weight:900;color:rgba(255,255,255,.05);font-family:Inter">{num}</span>
      <div style="display:flex;align-items:center;gap:10px">
        <span style="width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#3b6ef6,#8b5cf6 55%,#0fbf9a);display:grid;place-items:center;box-shadow:0 8px 20px -6px rgba(139,92,246,.5)">{ICONA[icona]}</span>
        <p style="margin:0;color:rgba(255,255,255,.5);font-size:13px;font-weight:700;letter-spacing:.14em;font-family:Inter">{titolo}</p>
      </div>
      <div style="margin-top:16px">{corpo}</div>{extra}
    </div>"""


def chips(cc):
    return "".join(
        f'<span style="border-radius:8px;background:{bg};color:{fg};font-weight:700;font-size:14px;padding:6px 12px;font-family:Inter">{t}</span>'
        for t, bg, fg in cc)


def feed(righe):
    out = []
    for badge, testo in righe:
        bg, fg = BADGE[badge]
        out.append(f"""
        <div style="display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.07)">
          <span style="border-radius:6px;background:{bg};color:{fg};font-weight:800;font-size:10.5px;padding:3px 8px;letter-spacing:.06em;font-family:Inter;flex-shrink:0">{badge}</span>
          <span style="color:rgba(255,255,255,.85);font-size:14.5px;font-weight:600;font-family:Inter;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{testo}</span>
        </div>""")
    return "".join(out)


def pagina(chip1, righe_feed):
    c1 = card("01", "filtro", "YOU SET THE FILTERS",
              f'<p style="margin:0;color:white;font-size:23px;font-weight:800;line-height:1.3;font-family:Inter">No URLs to paste.<br>Just what you need.</p>'
              f'<div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap">{chips(chip1)}</div>')
    c2 = card("02", "indice", "THE INDEX, REFRESHED DAILY",
              f'<div style="margin:-4px -6px">{feed(righe_feed)}</div>'
              '<p style="margin:12px 0 0;color:rgba(255,255,255,.55);font-size:13.5px;font-weight:600;font-family:Inter">closed leaves the source &rarr; closes here</p>')
    c3 = card("03", "download", "YOU GET CLEAN ROWS",
              '<p style="margin:0;color:white;font-size:23px;font-weight:800;line-height:1.3;font-family:Inter">In seconds, not minutes.</p>'
              f'<div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap">{chips([("JSON", "rgba(94,139,255,.18)", "#8ab8ff"), ("CSV", "rgba(167,139,250,.18)", "#c4b5fd"), ("Excel", "rgba(47,224,182,.18)", "#2fe0b6")])}</div>'
              '<p style="margin:14px 0 0;color:#2fe0b6;font-size:14px;font-weight:700;font-family:Inter">every field with its source</p>')
    return BASE_HEAD + f"""
<div style="width:1600px;height:500px;position:relative;overflow:hidden;background:#020617;font-family:Inter,sans-serif">
  <div style="position:absolute;top:-170px;left:28%;width:620px;height:620px;border-radius:50%;background:#8b5cf6;opacity:.16;filter:blur(120px)"></div>
  <div style="position:absolute;bottom:-220px;right:-100px;width:520px;height:520px;border-radius:50%;background:#0fbf9a;opacity:.12;filter:blur(110px)"></div>
  <div style="position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,.05) 1.2px,transparent 1.2px);background-size:28px 28px"></div>
  <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;gap:34px">
    {c1}{FRECCIA.format(id="a")}{c2}{FRECCIA.format(id="b")}{c3}
  </div>
</div></body></html>"""


VARIANTI = {
    "principale": (
        [("country: IT", "rgba(94,139,255,.18)", "#8ab8ff"),
         ("technology: SAP", "rgba(167,139,250,.18)", "#c4b5fd")],
        [("NEW", "Supply Chain Analyst, SAP · Rotterdam"),
         ("UPDATED", "Registered Nurse, EPIC · Austin"),
         ("CLOSED", "Warehouse Operative, forklift · Lille")]),
    "workday": (
        [("ats: workday", "rgba(47,224,182,.18)", "#2fe0b6"),
         ("country: DE", "rgba(94,139,255,.18)", "#8ab8ff")],
        [("NEW", "Manager, Finance &amp; Strategy · Flextronics"),
         ("UPDATED", "Pflegefachkraft (m/w/d) · Stuttgart"),
         ("CLOSED", "Werkstudent Quality · Quality Mgmt")]),
    "greenhouse": (
        [("ats: greenhouse", "rgba(47,224,182,.18)", "#2fe0b6"),
         ("remote", "rgba(94,139,255,.18)", "#8ab8ff")],
        [("NEW", "Analytics Engineer · VTEX"),
         ("NEW", "Backend Engineer, Python · Berlin"),
         ("CLOSED", "Biomedical Scientist · Hertfordshire")]),
    "tecnologie": (
        [("technology: Snowflake", "rgba(167,139,250,.18)", "#c4b5fd"),
         ("posted-after", "rgba(94,139,255,.18)", "#8ab8ff")],
        [("NEW", "Delivery Manager · Hanover"),
         ("NEW", "Senior IT Data Engineer · Springdale"),
         ("UPDATED", "Senior BI Analyst · Cape Town")]),
}

if __name__ == "__main__":
    for nome, (cc, righe) in VARIANTI.items():
        open(f"vetrina/_come_{nome}.html", "w").write(pagina(cc, righe))
        print("generata:", nome)
