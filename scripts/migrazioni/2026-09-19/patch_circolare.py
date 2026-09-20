"""Il livello 2 non puo' confermare un candidato costruito dal nome.

Difetto mio, trovato guardando il campione: i domini di livello 2 erano quasi
tutti della fonte `nome-generato`. Quella fonte COSTRUISCE `nomeazienda.com` dal
nome; poi il livello 2 diceva «il dominio corrisponde al nome» — che e' vero per
costruzione. Un ragionamento circolare che non verifica niente, e infatti ha
scritto `rm-int-rim-la-rochelle.com` (accento mangiato), `dessange-5c.com`
(residuo di codifica URL), `federation-admr-de-la-sarthe.com`.

Due condizioni nuove, e servono entrambe:
  1. il candidato deve venire da una fonte INDIPENDENTE dal nome — un motore di
     ricerca, l'email nell'annuncio, il logo. Non dal generatore di nomi.
  2. il dominio deve almeno ESISTERE: una risoluzione DNS, che costa nulla e non
     la blocca nessun Cloudflare.
"""
import ast
import pathlib

p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()

VECCHIO = '''        if nome:
            migliore, punteggio, da = None, 0.0, None
            for nome_fonte, dom in visti:
                v = somiglia_al_nome(nome, dom)
                if v > punteggio:
                    migliore, punteggio, da = dom, v, nome_fonte
            if migliore and punteggio >= SOGLIA_NOME:
                return (cid, migliore, da, f"nome~{punteggio:.2f}", 2)'''

NUOVO = '''        if nome:
            migliore, punteggio, da = None, 0.0, None
            for nome_fonte, dom in visti:
                # `nome-generato` COSTRUISCE il dominio dal nome: confermarlo col
                # nome sarebbe circolare. Valgono solo le fonti indipendenti.
                if nome_fonte == "nome-generato":
                    continue
                v = somiglia_al_nome(nome, dom)
                if v > punteggio:
                    migliore, punteggio, da = dom, v, nome_fonte
            # e deve almeno esistere: il DNS costa nulla e non lo blocca nessuno
            if migliore and punteggio >= SOGLIA_NOME and esiste(migliore):
                return (cid, migliore, da, f"nome~{punteggio:.2f}", 2)'''

ESISTE = '''

def esiste(dominio: str) -> bool:
    """Il dominio risolve? Una domanda al DNS, non una richiesta HTTP.

    Serve perche' un candidato plausibile puo' semplicemente non esistere, e il
    DNS lo dice in millisecondi senza che Cloudflare possa rispondere 403.
    """
    import socket
    for h in (dominio, "www." + dominio):
        try:
            socket.getaddrinfo(h, None)
            return True
        except OSError:
            continue
    return False

'''

if "def esiste(" in s:
    print("gia' applicato")
else:
    assert VECCHIO in s, "la cascata non e' quella attesa"
    s = s.replace(VECCHIO, NUOVO, 1)
    i = s.find("\nclass Cacciatore")
    s = s[:i] + ESISTE + s[i:]
    p.write_text(s)
    ast.parse(s)
    print("circolarita' rimossa e controllo DNS aggiunto")

import importlib.util
spec = importlib.util.spec_from_file_location("cd", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("\nprova del controllo DNS:")
for d, atteso in (("cvshealth.com", True), ("besix.com", True),
                  ("rm-int-rim-la-rochelle.com", False),
                  ("federation-admr-de-la-sarthe.com", False),
                  ("dessange-5c.com", False)):
    v = m.esiste(d)
    print(f"  {'ok ' if v == atteso else 'NO '} {d:<34}{'esiste' if v else 'non esiste'}")
