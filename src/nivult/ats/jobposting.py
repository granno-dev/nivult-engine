"""Il JobPosting di schema.org, letto come lo legge Google for Jobs.

Sono le funzioni pure (niente database, niente stato) che servono a
chiunque legga annunci pubblicati in JSON-LD: le agenzie per il lavoro
(`agenzie.py`, da cui vengono) e il lettore generico delle career page
(`jsonld.py`, adapter `jsonld`). Tenerle in un posto solo evita che i
due lettori divergano su come si trova un JobPosting dentro un @graph o
su come si perdona un JSON scritto a mano.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("nivult.ats.jobposting")


def _loc(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def _sitemap_urls(client: httpx.Client, sitemaps: list[str], massimo: int = 50000) -> set[str]:
    """Gli URL offerta, seguendo un livello di sitemapindex se serve."""
    urls: set[str] = set()
    for sm in sitemaps:
        try:
            corpo = client.get(sm).text
        except httpx.HTTPError as e:
            log.warning("sitemap %s: %s", sm, e)
            continue
        voci = _loc(corpo)
        if "<sitemapindex" in corpo:
            for figlio in voci:
                try:
                    urls.update(_loc(client.get(figlio).text))
                except httpx.HTTPError as e:
                    log.warning("sitemap figlia %s: %s", figlio, e)
                if len(urls) >= massimo:
                    break
        else:
            urls.update(voci)
        if len(urls) >= massimo:
            break
    return urls


def _jobposting(doc):
    """Trova il JobPosting in un documento ld+json (lista/@graph/dict)."""
    if isinstance(doc, list):
        for d in doc:
            jp = _jobposting(d)
            if jp:
                return jp
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("@type") in ("JobPosting", ["JobPosting"]):
        return doc
    return _jobposting(doc.get("@graph"))


def _estrai_ld(html: str):
    for blocco in re.findall(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
            html, re.S | re.I):
        try:
            jp = _jobposting(json.loads(blocco.strip()))
        except ValueError:
            # il JSON-LD e' spesso scritto a mano: commenti «//» e virgole
            # pendenti. Google li perdona, quindi li perdoniamo anche noi —
            # in forma conservativa: righe-commento intere e virgole prima
            # di } o ], mai dentro le stringhe (gli https:// ringraziano)
            pulito = "\n".join(r for r in blocco.strip().splitlines()
                               if not r.lstrip().startswith("//"))
            pulito = re.sub(r",\s*([}\]])", r"\1", pulito)
            try:
                jp = _jobposting(json.loads(pulito))
            except ValueError:
                continue
        if jp:
            return jp
    return None


def _data(jp) -> datetime | None:
    for chiave in ("datePosted", "datePublished"):
        v = jp.get(chiave)
        if not v:
            continue
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _luogo(jp) -> tuple[str | None, str | None]:
    posti = jp.get("jobLocation")
    if isinstance(posti, dict):
        posti = [posti]
    for p in posti or []:
        ind = (p or {}).get("address") or {}
        if isinstance(ind, str):
            return ind.strip() or None, None
        citta = (ind.get("addressLocality") or "").strip() or None
        paese = (ind.get("addressCountry") or "").strip() or None
        if isinstance(paese, dict):
            paese = (paese.get("name") or "").strip() or None
        if citta or paese:
            return citta, (paese[:2].upper() if paese else None)
    return None, None


def _testo(jp) -> str:
    d = jp.get("description")
    if isinstance(d, dict):
        d = d.get("@value") or ""
    return str(d or "")
