"""Chi legge l'esito della caccia adesso riceve anche il LIVELLO, e lo scrive.

`caccia()` restituisce cinque valori invece di quattro. Senza aggiornare qui il
demone si romperebbe all'istante con un errore di spacchettamento — ed e' il tipo
di rottura fortunata, perche' e' rumorosa. Quella silenziosa sarebbe stata
scrivere il livello sbagliato.
"""
import ast
import pathlib

p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()

VECCHIO = '''            scritte = [(d, f, cid) for cid, d, f, _ in esiti if d]
            for _, _, f, _ in [e for e in esiti if e[1]]:
                per_fonte[f] = per_fonte.get(f, 0) + 1
            if scritte and not a.dry_run:
                with c.cursor() as cur:
                    cur.executemany(
                        "UPDATE ats_companies SET site_domain = %s, site_domain_source = %s, "
                        "site_checked_at = now() WHERE id = %s AND site_domain IS NULL", scritte)
            st["trovati"] += len(scritte)
            dt = max(time.time() - t0, 1e-6)
            print(f"caccia: viste {st['viste']} | trovati {st['trovati']} "
                  f"({100*st['trovati']/max(st['viste'],1):.0f}%) | {per_fonte} | "
                  f"{int(3600*st['trovati']/dt)}/ora", flush=True)'''

NUOVO = '''            scritte = [(d, f, liv, cid) for cid, d, f, _, liv in esiti if d]
            for _, _, f, _, liv in [e for e in esiti if e[1]]:
                per_fonte[f] = per_fonte.get(f, 0) + 1
                st[f"livello{liv}"] = st.get(f"livello{liv}", 0) + 1
            if scritte and not a.dry_run:
                with c.cursor() as cur:
                    cur.executemany(
                        "UPDATE ats_companies SET site_domain = %s, site_domain_source = %s, "
                        "site_domain_livello = %s, site_checked_at = now() "
                        "WHERE id = %s AND site_domain IS NULL", scritte)
            st["trovati"] += len(scritte)
            dt = max(time.time() - t0, 1e-6)
            print(f"caccia: viste {st['viste']} | trovati {st['trovati']} "
                  f"({100*st['trovati']/max(st['viste'],1):.0f}%) | {per_fonte} | "
                  f"liv1 {st.get('livello1', 0)} liv2 {st.get('livello2', 0)} | "
                  f"{int(3600*st['trovati']/dt)}/ora", flush=True)'''

if "site_domain_livello" in s:
    print("gia' applicato")
else:
    assert VECCHIO in s, "il punto di scrittura non e' quello atteso"
    p.write_text(s.replace(VECCHIO, NUOVO, 1))
    ast.parse(p.read_text())
    print("scrittura del livello inserita")

import importlib.util
spec = importlib.util.spec_from_file_location("cd", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("il modulo si carica, e la caccia restituisce:",
      m.Cacciatore.caccia.__doc__.splitlines()[0][:60])
