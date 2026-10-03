# Il ricevitore dei webhook wuzapi: tre righe di Python, psycopg per la
# tabella wa_inbox. Si costruisce sul N5: docker build -t nivult-wa-webhook .
FROM python:3.12-alpine
RUN pip install --no-cache-dir "psycopg[binary]"
COPY wa-webhook.py /app/wa-webhook.py
EXPOSE 8079
CMD ["python", "/app/wa-webhook.py"]
