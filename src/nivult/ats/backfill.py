from __future__ import annotations

import time


def io_avg10() -> float:
    try:
        with open("/proc/pressure/io") as f:
            prima = f.readline().split()
        return float(dict(campo.split("=") for campo in prima[1:])["avg10"])
    except (OSError, ValueError, KeyError):
        return 0.0


def attendi(conn, max_outbox: int = 5000, max_io: float = 35.0) -> None:
    while True:
        outbox = conn.execute("SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0]
        io = io_avg10()
        if outbox <= max_outbox and io <= max_io:
            return
        time.sleep(10)
