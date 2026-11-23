"""Metriche di comportamento reale, senza eventi inventati."""
from __future__ import annotations


def metriche_reali(conn, giorni: int = 30) -> dict:
    if giorni <= 0:
        raise ValueError("giorni deve essere positivo")
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM digest_items di
            JOIN digests d ON d.id = di.digest_id
            WHERE d.sent_at > now() - make_interval(days => %s)
              AND di.match_id IS NOT NULL""", (giorni,))
        consegnate = cur.fetchone()[0]
        cur.execute("""SELECT count(*) AS valutate,
            count(*) FILTER (WHERE saved_at IS NOT NULL) AS salvate,
            count(*) FILTER (WHERE dismissed_at IS NOT NULL) AS scartate,
            count(*) FILTER (WHERE archived_at IS NOT NULL) AS archiviate,
            count(*) FILTER (WHERE application_status <> 'not_started') AS candidature
            FROM matches
            WHERE evaluated_at > now() - make_interval(days => %s)
               OR user_action_at > now() - make_interval(days => %s)""",
            (giorni, giorni))
        riga = cur.fetchone()
        cur.execute("""SELECT coalesce(provider, 'unknown'), coalesce(model, 'unknown'),
            count(*) FROM matches
            WHERE evaluated_at > now() - make_interval(days => %s)
            GROUP BY 1, 2 ORDER BY 3 DESC""", (giorni,))
        provider = [{"provider": p, "model": m, "valutate": n}
                    for p, m, n in cur.fetchall()]
    valutate, salvate, scartate, archiviate, candidature = riga
    return {
        "periodo_giorni": giorni,
        "consegnate": consegnate,
        "valutate": valutate,
        "salvate": salvate,
        "scartate": scartate,
        "archiviate": archiviate,
        "candidature": candidature,
        "tasso_salvate": salvate / consegnate if consegnate else None,
        "tasso_candidature": candidature / consegnate if consegnate else None,
        "provider": provider,
    }
