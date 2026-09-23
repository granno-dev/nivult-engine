"""Il data layer dell'API clienti: un DuckDB al giorno, DAGLI EXPORT.

`aggiorna` costruisce /opt/nivult/exports/api-clienti.duckdb ogni
mattina (in modo atomico), `dati` lo legge in sola lettura con
paginazione a chiave. Il Postgres di produzione non entra mai in
questa storia: regge gia' carico 20 coi demoni e i clienti non devono
poterlo rallentare.
"""
