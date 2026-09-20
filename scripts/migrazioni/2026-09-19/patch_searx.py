"""SearXNG: chiedere solo ai motori che rispondono, e non insistere quando tace.

Il 19/09/2026 SearXNG ha prodotto **1 dominio su 691**, quando misurato da solo
rendeva il 18%. Non era rotto: rispondeva 200 con ZERO risultati, e nessuno se ne
accorgeva perche' 200 sembra un successo.

Provati i motori uno per uno dall'IP di casa:
  google      10 risultati, il primo e' www.airwallex.com      <- funziona
  yahoo        7 risultati, il primo e' www.airwallex.com      <- funziona
  bing        10 risultati ma fuori tema (parser rotto)
  duckduckgo   0 — timeout a ogni richiesta su html.duckduckgo.com
  brave, mojeek, startpage, qwant, presearch, wikipedia:  0

Con l'insieme predefinito la ricerca aspetta i morti e, quando Google e' in
sospensione temporanea («unusual traffic from your network», 180 s), resta zero.
Chiedendo solo a google,yahoo si hanno risultati e la risposta arriva prima.
"""
import ast
import pathlib

p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()

VECCHIO = '''    def da_searx(self, nome, paese) -> list[str]:
        q = urllib.parse.quote(f"{nome} {paese} official website careers".strip()[:120])
        try:
            rq = urllib.request.Request(f"{SEARX}?q={q}&format=json", headers={"User-Agent": "nivult/1.0"})
            with urllib.request.urlopen(rq, timeout=40) as r:
                d = json.load(r)
        except Exception:
            return []'''

NUOVO = '''    def da_searx(self, nome, paese) -> list[str]:
        q = urllib.parse.quote(f"{nome} {paese} official website careers".strip()[:120])
        # Solo i motori che rispondono davvero da questo IP (misurato il 19/09/2026):
        # gli altri dell'insieme predefinito danno zero e fanno aspettare il timeout.
        try:
            rq = urllib.request.Request(f"{SEARX}?q={q}&format=json&engines={MOTORI_SEARX}",
                                        headers={"User-Agent": "nivult/1.0"})
            with urllib.request.urlopen(rq, timeout=40) as r:
                d = json.load(r)
        except Exception:
            return []
        # Una risposta 200 con zero risultati NON e' un successo: vuol dire che i
        # motori ci hanno bloccati. Si conta, cosi' si vede nei numeri del demone
        # invece di sparire in silenzio come e' successo per 691 aziende.
        if not d.get("results"):
            self.searx_vuote = getattr(self, "searx_vuote", 0) + 1'''

if "MOTORI_SEARX" in s:
    print("gia' applicato")
else:
    assert VECCHIO in s, "il testo di da_searx non e' quello atteso"
    s = s.replace(VECCHIO, NUOVO, 1)
    s = s.replace('SEARX = os.environ.get("SEARX_URL", "http://100.119.200.7:8899/search")',
                  'SEARX = os.environ.get("SEARX_URL", "http://100.119.200.7:8899/search")\n'
                  'MOTORI_SEARX = os.environ.get("MOTORI_SEARX", "google,yahoo")', 1)
    p.write_text(s)
    ast.parse(s)
    print("applicato")

import importlib.util
spec = importlib.util.spec_from_file_location("cd", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("MOTORI_SEARX =", m.MOTORI_SEARX)
print("SEARX        =", m.SEARX)
