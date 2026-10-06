# Apify Actor: ingresso dell'Actor, nient'altro che plumbing.
#
# L'utente Apify sceglie i filtri, l'Actor interroga la NOSTRA API v1
# con la chiave interna (mai esposta: sta nei segreti dell'Actor) e
# riversa le righe nel dataset. I crediti dell'utente li conta Apify
# (pay-per-result); i nostri li conta la chiave interna.
from __future__ import annotations

import json
import os

import httpx
from apify import Actor

API = os.environ.get("NIVULT_API_URL", "https://api.nivult.com")
FILTRI_JOBS = ("country", "category", "ats", "seniority", "remote",
               "language", "q", "technology", "dal")
FILTRI_AZIENDE = ("country", "industry", "q", "technology",
                  "employees_min", "employees_max")
TETTO = 10_000          # oltre, il canale giusto e' l'export giornaliero


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        chiave = os.environ.get("NIVULT_API_KEY")
        if not chiave:
            raise RuntimeError("NIVULT_API_KEY mancante: e' il segreto "
                               "dell'Actor, si configura nella console")
        tipo = inp.get("tipo", "jobs")
        endpoint = "companies" if tipo == "companies" else "jobs"
        ammessi = FILTRI_AZIENDE if tipo == "companies" else FILTRI_JOBS
        filtri = {k: v for k, v in inp.items()
                  if k in ammessi and v not in (None, "")}
        # le nicchie dello store (un Actor per ATS/tecnologia): lo STESSO
        # codice con un filtro preimpostato via env. Il filtro dell'utente
        # vince se copre lo stesso campo — il preset non ingabbia mai.
        preset = json.loads(os.environ.get("NIVULT_PRESET", "{}"))
        for k, v in preset.items():
            if k in ammessi and not filtri.get(k):
                filtri[k] = v
        limite = min(int(inp.get("limite") or 100), TETTO)
        cursor = None
        scritti = 0
        async with httpx.AsyncClient(timeout=60) as cli:
            while scritti < limite:
                params = {**filtri, "limit": min(500, limite - scritti)}
                if cursor:
                    params["cursor"] = cursor
                r = await cli.get(f"{API}/v1/{endpoint}", params=params,
                                  headers={"X-Api-Key": chiave})
                r.raise_for_status()
                d = r.json()
                righe = d.get("data") or []
                if not righe:
                    break
                await Actor.push_data(righe)
                scritti += len(righe)
                cursor = d.get("next_cursor")
                if not cursor:
                    break
                Actor.log.info("… %d righe scritte", scritti)
        await Actor.set_value("RIEPILOGO", {"righe": scritti,
                                            "tipo": tipo,
                                            "filtri": filtri})
        Actor.log.info("finito: %d righe (%s)", scritti, tipo)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
