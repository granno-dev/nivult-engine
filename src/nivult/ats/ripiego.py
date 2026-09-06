"""Il ripiego: quando l'adapter tace, la pagina parla lo stesso.

Un adapter che torna zero offerte su un tenant che ne aveva e' quasi
sempre un template cambiato (JazzHR, 06/09/2026: 33.344 offerte vive
scadute). Prima che qualcuno lo ripari, questo modulo guarda la pagina
che l'adapter ha scaricato e cerca dentro le offerte che ABBIAMO GIA' in
archivio: se il loro URL c'e' ancora, l'offerta e' ancora li', e si
rinfresca `fetched_at`. Non importa offerte nuove — non saprebbe dare
loro l'`external_id` giusto, e alla riparazione arriverebbero doppie —
conferma solo la presenza di quelle note. E' abbastanza perche' nessuna
scada per un adapter muto.

Niente rete, niente modello: solo testo e archivio.
"""
from __future__ import annotations

import html as html_mod
import logging
import re

log = logging.getLogger("nivult.ats.ripiego")


def _varianti(url: str) -> set[str]:
    """Le forme con cui lo stesso URL puo' comparire in una pagina."""
    u = url.strip()
    out = {u}
    senza_query = u.split("?", 1)[0].split("#", 1)[0]
    out.add(senza_query)
    for x in list(out):
        out.add(x.rstrip("/"))
        out.add(re.sub(r"^https?://", "", x))
        out.add(re.sub(r"^https?://(www\.)?", "", x))
    return {v for v in out if len(v) > 12}


def conferma(conn, platform_id: str, slug: str, pagina: str) -> tuple[int, int]:
    """Ritorna (attive_in_archivio, confermate). Rinfresca le confermate."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, url FROM ats_jobs WHERE platform_id=%s AND slug=%s AND expired_at IS NULL",
                    (platform_id, slug))
        righe = cur.fetchall()
    if not righe or not pagina:
        return len(righe), 0
    testo = html_mod.unescape(pagina)
    trovate = []
    for jid, url in righe:
        if not url:
            continue
        if any(v in testo for v in _varianti(url)):
            trovate.append(jid)
    if trovate:
        with conn.cursor() as cur:
            cur.execute("UPDATE ats_jobs SET fetched_at = now(), expired_at = NULL WHERE id = ANY(%s)", (trovate,))
        conn.commit()
    log.info("  ripiego %s/%s: %d offerte in archivio, %d ritrovate nella pagina",
             platform_id, slug, len(righe), len(trovate))
    return len(righe), len(trovate)
