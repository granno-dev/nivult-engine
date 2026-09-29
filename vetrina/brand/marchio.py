#!/usr/bin/env python3
"""nivult. come marchio: il nome intero in tracciati SVG veri + il puntino menta.
Tre candidati tipografici, peso 700, caccia stretta ma onesta."""
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.varLib.instancer import instantiateVariableFont
import os

FONTI = [("space-grotesk", "Space Grotesk"), ("bricolage", "Bricolage Grotesque"), ("sora", "Sora")]
PAROLA = "nivult"
PESO = 700
CASCATA = 0  # niente spazi extra: il font respira da solo

def path_della_parola(font):
    """La parola come UN path SVG, composta glifo per glifo."""
    glyf = font.getGlyphSet()
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    tracciati, x = [], 0.0
    for ch in PAROLA:
        gname = cmap[ord(ch)]
        pen = SVGPathPen(glyf)
        glyf[gname].draw(pen)
        d = pen.getCommands()
        if d:
            tracciati.append(f'<path d="{d}" transform="translate({x:.1f},0)"/>')
        x += glyf[gname].width + CASCATA
    larghezza = x - CASCATA
    return "".join(tracciati), larghezza, upm

def svg_marchio(nome_file, d, larghezza, upm):
    """Il marchio: la parola in inchiostro scura e il puntino in gradiente di marca."""
    scala = 512 / upm
    W = int(larghezza * scala)
    H = 512
    r = int(upm * 0.135 * scala)
    gap = int(upm * 0.025 * scala)
    cx_dot = W + gap + r
    cy_dot = int(H * 0.82)
    Wtot = cx_dot + r + int(upm * 0.02 * scala)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {Wtot} {H}">
  <defs>
    <linearGradient id="punto" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#3b6ef6"/><stop offset="1" stop-color="#0fbf9a"/>
    </linearGradient>
  </defs>
  <g transform="scale({scala},{-scala}) translate(0,{-upm*0.82:.0f})" fill="#0f172a">{d}</g>
  <circle cx="{cx_dot}" cy="{cy_dot}" r="{r}" fill="url(#punto)"/>
</svg>''', Wtot

for fname, bello in FONTI:
    f = TTFont(f"/tmp/fonti/{fname}.ttf")
    if "fvar" in f:
        f = instantiateVariableFont(f, {"wght": PESO})
    d, larghezza, upm = path_della_parola(f)
    svg, Wtot = svg_marchio(fname, d, larghezza, upm)
    out = f"/tmp/fonti/{fname}.svg"
    open(out, "w").write(svg)
    print(f"{bello}: {Wtot}px di larghezza")
    os.system(f"cd /tmp/fonti && qlmanage -t -s 1024 -o /tmp/fonti {fname}.svg >/dev/null 2>&1")
print("fatto")
