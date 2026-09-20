"""La cascata accetta il livello 2 quando il livello 1 non ce la fa.

Prima: si scriveva un dominio SOLO se il sito rimandava al nostro tenant. Chi non
lo supera restava vuoto — e fra questi c'erano CVS Health, Broadcom e Colliers,
che un dominio ovvio ce l'hanno.

Adesso, in ordine:
  1. si prova il livello 1 su TUTTE le fonti, come prima. E' la prova migliore e
     prende cose che il nome non direbbe mai (Orbotech -> kla.com).
  2. solo se nessuna passa, si guarda fra i candidati gia' raccolti se ce n'e'
     uno che CORRISPONDE al nome dell'azienda (>= 0,80). Niente rete in piu': i
     candidati sono gia' in mano.

Il livello si scrive sempre in archivio: chi compra deve poter scegliere.
"""
import ast
import pathlib

p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()

VECCHIO = '''        for nome_fonte, prendi in fonti:
            for dom in prendi():
                via = self.verifica(dom, patt)
                if via:
                    return (cid, dom, nome_fonte, via)
        return (cid, None, None, None)'''

NUOVO = '''        visti = []                       # i candidati raccolti strada facendo
        for nome_fonte, prendi in fonti:
            for dom in prendi():
                visti.append((nome_fonte, dom))
                via = self.verifica(dom, patt)
                if via:
                    return (cid, dom, nome_fonte, via, 1)
        # Livello 1 fallito. Fra i candidati che abbiamo gia' in mano — nessuna
        # richiesta in piu' — ce n'e' uno che dice il nome dell'azienda?
        # Il nome dell'azienda, non lo slug del tenant: lo slug e' spesso una
        # storpiatura («Cvshealth», «Colliersinternationalemea») e farebbe
        # passare corrispondenze che non sono tali.
        if nome:
            migliore, punteggio, da = None, 0.0, None
            for nome_fonte, dom in visti:
                v = somiglia_al_nome(nome, dom)
                if v > punteggio:
                    migliore, punteggio, da = dom, v, nome_fonte
            if migliore and punteggio >= SOGLIA_NOME:
                return (cid, migliore, da, f"nome~{punteggio:.2f}", 2)
        return (cid, None, None, None, None)'''

if "SOGLIA_NOME:" in s and "visti = []" in s:
    print("gia' applicato")
else:
    assert VECCHIO in s, "la cascata non e' quella attesa"
    s = s.replace(VECCHIO, NUOVO, 1)
    p.write_text(s)
    ast.parse(s)
    print("cascata aggiornata")

# chi consuma il risultato si aspettava 4 valori, adesso sono 5
s = p.read_text()
print("\npunti da aggiornare (chi legge il risultato della caccia):")
for i, riga in enumerate(s.split("\n"), 1):
    if "caccia" in riga and ("for " in riga or "= " in riga) and "def caccia" not in riga:
        print(f"  {i}: {riga.strip()[:100]}")
