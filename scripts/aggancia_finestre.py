"""Aggancia la lettura a pezzi al demone delle tecnologie.

Tre modifiche, e nessuna tocca il modello:

1. via il taglio a 20.000 caratteri scritto a mano;
2. ogni annuncio diventa una o piu' FINESTRE, ciascuna una riga del lotto;
3. le marcature delle finestre dello stesso annuncio si uniscono.

L'impronta del testo (per le gemelle) resta quella del testo INTERO: due
annunci identici hanno la stessa risposta a prescindere da come li leggiamo.
"""
from __future__ import annotations
import pathlib
import sys

P = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                 else "/opt/nivult/engine/scripts/tec_v1_demone.py")
s = P.read_text()
fatte = []

# `finestre` si carica per percorso esplicito come gia' fa `ancoraggio`: sul
# Mac mini il demone gira da una cartella che non sta in sys.path, e un import
# normale funzionerebbe qui e fallirebbe la'.
ANCORA = "_sp.loader.exec_module(ancoraggio)"
CARICA = ANCORA + """

_sf = importlib.util.spec_from_file_location(
    "finestre", str(pathlib.Path(__file__).resolve().parent / "finestre.py"))
_finestre_mod = importlib.util.module_from_spec(_sf)
_sf.loader.exec_module(_finestre_mod)
finestre = _finestre_mod.finestre"""
if "_finestre_mod" not in s:
    assert ANCORA in s, "il caricamento di ancoraggio non combacia"
    s = s.replace(ANCORA, CARICA, 1)
    fatte.append("import")

vecchio_taglio = 'lavoro.append((jid, f"{titolo or \'\'}\\n{testo}"[:20000]))'
nuovo_taglio = (
    '# NIENTE TAGLIO A MANO. Il testo entra intero e piu\' sotto viene letto a\n'
    '                # pezzi: a 1024 token il 22,5% degli annunci veniva letto a\n'
    '                # meta\', e su quelli si perdeva il 27% del testo — la fine,\n'
    '                # dove i requisiti elencano gli strumenti (20/09/2026).\n'
    '                lavoro.append((jid, f"{titolo or \'\'}\\n{testo}"))')
if vecchio_taglio in s:
    s = s.replace(vecchio_taglio, nuovo_taglio)
    fatte.append("taglio")

vecchio_ciclo = """                da_fare.sort(key=lambda x: len(x[1]))
                with torch.inference_mode():
                    for i in range(0, len(da_fare), a.lotto):
                        gruppo = da_fare[i:i + a.lotto]
                        enc = tok([t for _, t, _ in gruppo], truncation=True,
                                  max_length=a.max_len, padding=True,
                                  return_offsets_mapping=True, return_tensors="pt")
                        off = enc.pop("offset_mapping")
                        pr = torch.softmax(
                            mod(**{k: v.to(dev) for k, v in enc.items()}).logits, -1).cpu()
                        for j2, (jid, testo, h) in enumerate(gruppo):
                            tec = voci(testo, pr[j2], off[j2].tolist(), SOGLIA)
                            st["nomi"] += len(tec)
                            st["vuote"] += not tec
                            scritte.append((jid, Jsonb(tec), len(tec), h))"""

nuovo_ciclo = """                # OGNI ANNUNCIO DIVENTA UNA O PIU' FINESTRE, e il lotto si forma
                # su quelle. Un annuncio lungo costa piu' di uno corto, che e'
                # giusto: prima costava uguale perche' lo leggevamo a meta'.
                pezzi = []
                for jid, testo, h in da_fare:
                    for k, f in enumerate(finestre(tok, testo, a.max_len)):
                        pezzi.append((jid, f, h, k))
                st["finestre"] = st.get("finestre", 0) + len(pezzi)
                st["spezzati"] = st.get("spezzati", 0) + sum(
                    1 for _, _, _, k in pezzi if k == 1)

                trovate = {j: [] for j, _, _ in da_fare}
                pezzi.sort(key=lambda x: len(x[1]))
                with torch.inference_mode():
                    for i in range(0, len(pezzi), a.lotto):
                        gruppo = pezzi[i:i + a.lotto]
                        enc = tok([t for _, t, _, _ in gruppo], truncation=True,
                                  max_length=a.max_len, padding=True,
                                  return_offsets_mapping=True, return_tensors="pt")
                        off = enc.pop("offset_mapping")
                        pr = torch.softmax(
                            mod(**{k: v.to(dev) for k, v in enc.items()}).logits, -1).cpu()
                        for j2, (jid, finestra, h, _) in enumerate(gruppo):
                            for n in voci(finestra, pr[j2], off[j2].tolist(), SOGLIA):
                                if n not in trovate[jid]:
                                    trovate[jid].append(n)

                for jid, testo, h in da_fare:
                    tec = trovate[jid]
                    st["nomi"] += len(tec)
                    st["vuote"] += not tec
                    scritte.append((jid, Jsonb(tec), len(tec), h))"""

if vecchio_ciclo in s:
    s = s.replace(vecchio_ciclo, nuovo_ciclo)
    fatte.append("ciclo")
elif "pezzi.sort" in s:
    fatte.append("ciclo (gia' fatto)")
else:
    raise SystemExit("il ciclo non combacia: guardare a mano prima di toccare")

P.write_text(s)
print("modifiche:", ", ".join(fatte) or "nessuna")
