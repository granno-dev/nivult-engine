"""Il pronto soccorso: quando la sentinella trova un problema NOTO, lo cura
da sola e racconta su Telegram cosa ha fatto. Chiesto da Giuseppe il
2026-09-06 sera: «gli alert li ricevi tu, controlli, correggi e mi mandi
un messaggio di conferma».

Primo strato, senza AI: rimedi deterministici per i guasti che conosciamo
(un demone giu' si riavvia, uno sprint morto si rilancia, un backup non
spedito si rispedisce, un ponte fermo si rilancia). Per tutto il resto:
il messaggio arriva lo stesso, con la diagnosi che c'e', e il secondo
strato (una routine Claude nel cloud) ci ragiona sopra.

Regole: mai due volte la stessa cura nello stesso giro; ogni cura verifica
l'esito e lo dice; il messaggio Telegram parte solo se c'e' qualcosa di
nuovo (la sentinella gira ogni 5 minuti).
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import time

BASE = "/opt/nivult/engine"


def _sh(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()[-400:]
    except Exception as exc:                          # noqa: BLE001
        return 1, repr(exc)[:200]


def _riavvia(unita: str) -> str:
    _sh(["systemctl", "restart", unita])
    time.sleep(8)
    rc, out = _sh(["systemctl", "is-active", unita])
    return f"riavviato {unita}: {'attivo' if out.strip() == 'active' else 'ANCORA GIU (' + out.strip() + ')'}"


_CURABILI = ("demone nivult-", "scrape fermo", "sprint fermo", "BACKUP FALLITO", "backup:", "ponte", "adapter")


def curabile(problema: str) -> bool:
    return problema.startswith(_CURABILI)


def chiama_medico(problemi: list[str]) -> bool:
    """I problemi senza cura vanno al medico (Claude sul server), in
    background sotto systemd cosi' la sentinella non aspetta. Un'unita'
    sola per volta: se una visita e' in corso, si accoda al giro dopo."""
    if not problemi:
        return False
    rc, out = _sh(["systemd-run", "--unit=nivult-medico", "--collect", "--quiet",
                   f"{BASE}/deploy/medico.sh", *problemi], timeout=20)
    return rc == 0


def cura(problemi: list[str]) -> list[str]:
    """Per ogni problema nuovo, la cura se esiste. Ritorna le righe di
    resoconto (cosa fatto, con esito)."""
    fatte: list[str] = []
    gia: set[str] = set()
    for p in problemi:
        m = re.match(r"adapter (?:rotto|muto) ([a-z0-9_-]+)", p)
        if m and ("officina " + m.group(1)) not in gia:
            # L'OFFICINA: Claude sul server ripara l'adapter da un campione
            # della pagina, lo prova sul banco, e se passa lo deploya con
            # canarino e rollback. Limiti di frequenza dentro officina.sh.
            gia.add("officina " + m.group(1))
            rc, out = _sh(["systemd-run", f"--unit=nivult-officina-{m.group(1)}", "--collect", "--quiet",
                           f"{BASE}/deploy/officina.sh", m.group(1)], timeout=20)
            fatte.append(f"officina aperta su {m.group(1)}: {'avviata' if rc == 0 else 'NON avviata: ' + out}")
            continue
        m = re.match(r"demone (nivult-[\w-]+):", p)
        if m and m.group(1) not in gia:
            gia.add(m.group(1)); fatte.append(_riavvia(m.group(1))); continue
        if p.startswith("scrape fermo") and "scrape" not in gia:
            gia.add("scrape")
            fatte.append(_riavvia("nivult-scrape")); fatte.append(_riavvia("nivult-scrape-veloce")); continue
        if p.startswith("sprint fermo") and "sprint" not in gia:
            gia.add("sprint")
            rc, out = _sh([f"{BASE}/deploy/sprint.sh", "start"], timeout=30)
            fatte.append(f"sprint rilanciato: {out.splitlines()[-1] if out else rc}"); continue
        if (p.startswith("BACKUP FALLITO") or p.startswith("backup:")) and "backup" not in gia:
            gia.add("backup")
            oggi = time.strftime("%Y-%m-%d")
            esiste = os.path.exists(f"/opt/nivult/backups/nivult-{oggi}.sql.gz.enc")
            env = {**os.environ, "SOLO_INVIO": "1" if esiste else "0"}
            # in background sotto systemd: puo' durare minuti, e non deve morire con la sentinella
            rc, out = _sh(["systemd-run", "--unit=nivult-backup-cura", "--collect", "--quiet",
                           f"--setenv=SOLO_INVIO={env['SOLO_INVIO']}", "/opt/nivult/backup.sh"], timeout=20)
            fatte.append(f"backup {'rispedito (SOLO_INVIO)' if esiste else 'rifatto da capo'} in background: "
                         f"{'avviato' if rc == 0 else 'NON avviato: ' + out}"); continue
        if p.startswith("ponte") and "ponte" not in gia:
            gia.add("ponte")
            # lo script del cron, non il modulo Python: cosi' la corsa
            # rispetta il flock E scrive nello stesso log che la
            # sentinella guarda. Curare in un posto e controllare in un
            # altro lasciava l'incidente aperto per sempre a guasto
            # risolto — successo il 06/09, con 14 offerte davvero
            # importate e l'allarme ancora acceso.
            rc, out = _sh(["systemd-run", "--unit=nivult-ponte-cura", "--collect", "--quiet",
                           "/bin/bash", "-c",
                           f"{BASE}/deploy/ponte-ats.sh >> /var/log/nivult-ponte-ats.log 2>&1"], timeout=20)
            fatte.append(f"ponte rilanciato in background: {'avviato' if rc == 0 else 'NON avviato: ' + out}"); continue
        # per tutto il resto (N5 muto, scadenze anomale, memoria, disco, credito,
        # completezza) non c'e' una cura automatica sicura: si racconta e basta
    return fatte


def _chat_id() -> str | None:
    """Il chat id Telegram di Giuseppe: quello del suo utente nel motore."""
    try:
        import psycopg
        url = None
        for f in ("/opt/nivult/engine/.env", "/opt/nivult/.env"):
            try:
                m = re.search(r"^DATABASE_URL=(.*)$", open(f).read(), re.M)
                if m:
                    url = m.group(1).strip(); break
            except OSError:
                pass
        if not url:
            return None
        with psycopg.connect(url, connect_timeout=10) as c:
            r = c.execute("SELECT telegram_chat_id FROM users WHERE email=%s AND telegram_chat_id IS NOT NULL",
                          (os.environ.get("SENTINELLA_DESTINATARIO", "g.ranno@outlook.com"),)).fetchone()
            return str(r[0]) if r else None
    except Exception:                                 # noqa: BLE001
        return None


def _pronto_bot() -> str | None:
    for f in ("/opt/nivult/engine/.env", "/opt/nivult/.env"):
        try:
            m = re.search(r"^TELEGRAM_BOT_TOKEN=(.+)$", open(f).read(), re.M)
            if m:
                os.environ.setdefault("TELEGRAM_BOT_TOKEN", m.group(1).strip()); break
        except OSError:
            pass
    chat = _chat_id()
    return chat if chat and os.environ.get("TELEGRAM_BOT_TOKEN") else None


def _corpo(titolo: str, righe: list[str]) -> str:
    e = html.escape
    return "\n".join([f"<b>{e(titolo)}</b> · {time.strftime('%H:%M')} UTC"] + [e(r) for r in righe if r])[:3900]


def telegram_id(titolo: str, righe: list[str]) -> str | None:
    """Un messaggio per incidente: ritorna l'id, che serve per MODIFICARLO
    alla risoluzione invece di mandarne un altro."""
    try:
        from nivult.delivery.telegram import invia_testo
        chat = _pronto_bot()
        return invia_testo(chat, _corpo(titolo, righe)) if chat else None
    except Exception:                                 # noqa: BLE001
        return None


def telegram_modifica(message_id: str, titolo: str, righe: list[str]) -> bool:
    try:
        from nivult.delivery.telegram import _chiama
        chat = _pronto_bot()
        if not chat:
            return False
        _chiama("editMessageText", {"chat_id": chat, "message_id": int(message_id),
                                    "text": _corpo(titolo, righe), "parse_mode": "HTML",
                                    "link_preview_options": {"is_disabled": True}})
        return True
    except Exception:                                 # noqa: BLE001
        return False


def telegram(titolo: str, problemi: list[str], fatte: list[str], rientrati: list[str] | None = None) -> bool:
    """Il messaggio di conferma: cosa c'era, cosa e' stato fatto, cosa resta a Giuseppe."""
    try:
        for f in ("/opt/nivult/engine/.env", "/opt/nivult/.env"):
            try:
                m = re.search(r"^TELEGRAM_BOT_TOKEN=(.+)$", open(f).read(), re.M)
                if m:
                    os.environ.setdefault("TELEGRAM_BOT_TOKEN", m.group(1).strip()); break
            except OSError:
                pass
        from nivult.delivery.telegram import invia_testo
        chat = _chat_id()
        if not chat or not os.environ.get("TELEGRAM_BOT_TOKEN"):
            return False
        e = html.escape
        righe = [f"<b>{e(titolo)}</b> · {time.strftime('%H:%M')} UTC"]
        if problemi:
            righe.append("\n<b>Trovato:</b>\n" + "\n".join(f"• {e(p)}" for p in problemi))
        if fatte:
            righe.append("\n<b>Fatto:</b>\n" + "\n".join(f"• {e(f)}" for f in fatte))
        elif problemi:
            righe.append("\n<i>Nessuna cura automatica per questi: guardo io e ti dico.</i>")
        if rientrati:
            righe.append("\n<b>Rientrato:</b>\n" + "\n".join(f"• {e(r)}" for r in rientrati))
        invia_testo(chat, "\n".join(righe)[:3900])
        return True
    except Exception:                                 # noqa: BLE001
        return False
