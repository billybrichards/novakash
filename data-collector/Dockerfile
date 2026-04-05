FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# MODE env var: "collect" (default) or "backfill"
# collect = continuous live collection
# backfill = fetch 30 days history then exit
CMD ["sh", "-c", "if [ \"$MODE\" = 'backfill' ]; then python -u backfill.py; else python -u collector.py; fi"]
