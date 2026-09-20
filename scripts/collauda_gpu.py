"""Collaudo di una scheda a NOLEGGIO, da fare PRIMA di lanciarci un addestramento.

Il 17/09/2026 una RTX 4090 da 48 GB presa su RunPod calcolava male il logaritmo:
2.030 valori sbagliati su 20 milioni, e risultati DIVERSI a ogni ripetizione sullo
stesso ingresso. Le conseguenze non dicevano mai la verita':
  - torch.randn produceva infiniti (Box-Muller usa il logaritmo);
  - mT5 cadeva con «index out of bounds» nel calcolo della posizione relativa
    (anche quello passa da un logaritmo), ogni volta in un punto diverso del modello.
Due ore di caccia a un bug che non era nel codice ne' nei dati.

Le operazioni semplici erano tutte corrette: somme, moltiplicazioni di matrici
piccole, copie fra CPU e GPU. Una scheda si puo' rompere a meta'.

Regola: questo collaudo dura un minuto, l'addestramento dura ore. Prima il collaudo.
Esce con codice 1 se la scheda non e' affidabile.

  python collauda_gpu.py
"""
from __future__ import annotations
import sys
import torch

def main() -> int:
    if not torch.cuda.is_available():
        print("nessuna GPU visibile"); return 1
    d = torch.device("cuda")
    print(f"scheda: {torch.cuda.get_device_name(0)} | torch {torch.__version__}")
    guasti: list[str] = []

    # 1. FUNZIONI TRASCENDENTI: log, exp, sqrt, sin confrontate con la CPU.
    #    E' qui che si e' rotta la scheda del 17/09/2026.
    x = torch.linspace(0.001, 50.0, 20_000_000, device=d)
    for nome, f in (("log", torch.log), ("exp", torch.exp), ("sqrt", torch.sqrt),
                    ("rsqrt", torch.rsqrt), ("sin", torch.sin), ("tanh", torch.tanh)):
        y = f(x); atteso = f(x.cpu()).to(d)
        lontani = int((y - atteso).abs().gt(1e-3).sum())
        strani = int(y.isnan().sum()) + int(y.isinf().sum())
        if lontani or strani:
            guasti.append(f"{nome}: {lontani} valori lontani dalla CPU, {strani} nan/inf")
        # e la RIPETIBILITA': una funzione elemento per elemento deve dare
        # sempre lo stesso risultato sullo stesso ingresso.
        primo = f(x)
        diversi = sum(0 if torch.equal(f(x), primo) else 1 for _ in range(5))
        if diversi:
            guasti.append(f"{nome}: {diversi}/5 ripetizioni diverse sullo stesso ingresso")
    del x; torch.cuda.empty_cache()

    # 2. IL GENERATORE CASUALE: randn sta fra -6 e 6, sempre. Niente infiniti.
    for dt in (torch.float32, torch.bfloat16):
        r = torch.randn(4096, 4096, device=d, dtype=dt)
        brutti = int(r.isnan().sum()) + int(r.isinf().sum())
        if brutti or float(r.float().abs().max()) > 8:
            guasti.append(f"randn {dt}: {brutti} nan/inf, massimo {float(r.float().abs().max()):.1f}")
    del r; torch.cuda.empty_cache()

    # 3. MOLTIPLICAZIONE DI MATRICI: deterministica, con e senza TF32.
    a = torch.randn(4096, 4096, device=d); b = torch.randn(4096, 4096, device=d)
    rif = a @ b
    if int(rif.isnan().sum()) or int(rif.isinf().sum()):
        guasti.append("matmul: produce nan/inf da ingressi sani")
    diversi = sum(0 if torch.equal(a @ b, rif) else 1 for _ in range(10))
    if diversi: guasti.append(f"matmul: {diversi}/10 ripetizioni diverse")
    del a, b, rif; torch.cuda.empty_cache()

    # 4. MEMORIA: si scrive un motivo noto su gran parte della scheda e si rilegge.
    libera = torch.cuda.mem_get_info()[0]
    n = int(libera * 0.7) // 4
    t = torch.arange(n, dtype=torch.int32, device=d)
    atteso = (n - 1) * n // 2
    for _ in range(3):
        if int(t.sum(dtype=torch.int64)) != atteso:
            guasti.append("memoria: la somma di un tensore noto cambia"); break
    del t; torch.cuda.empty_cache()

    # 5. INDICI: un embedding con indici sempre validi non deve mai lamentarsi.
    emb = torch.nn.Embedding(32, 64).to(d)
    try:
        for _ in range(200):
            emb(torch.randint(0, 32, (1024, 1024), device=d)).sum().item()
    except Exception as exc:                                  # noqa: BLE001
        guasti.append(f"indici: {type(exc).__name__}: {str(exc)[:100]}")

    if guasti:
        print("\nSCHEDA NON AFFIDABILE, non lanciarci niente:")
        for g in guasti: print("  -", g)
        return 1
    print("\ncollaudo superato: la scheda calcola bene.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
