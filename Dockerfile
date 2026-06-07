# Dockerfile – Image für RSS-Aggregator und Sentiment-Worker (gemeinsames Image)
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

# CPU-only PyTorch zuerst (deutlich kleiner als die GPU-Standardvariante).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Restliche Abhängigkeiten
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Standard-Kommando; wird je Service in docker-compose überschrieben
CMD ["python", "-m", "src.rss_aggregator"]
