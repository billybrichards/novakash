# ─── TimesFM BTC Forecast Service ────────────────────────────────────────────
# Python 3.11, CPU-only PyTorch, TimesFM 2.5 200M

FROM python:3.11-slim

# ── System deps ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps ──────────────────────────────────────────────────────────────
# Copy requirements first for layer caching
COPY requirements.txt .

# Install CPU-only PyTorch first (much smaller than CUDA build)
RUN pip install --no-cache-dir \
    torch \
    --index-url https://download.pytorch.org/whl/cpu

# Install remaining deps
RUN pip install --no-cache-dir -r requirements.txt

# ── App code ─────────────────────────────────────────────────────────────────
COPY app/ ./app/

# ── Runtime ──────────────────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# Cache Hugging Face model in the container volume
ENV HF_HOME=/app/.cache/huggingface
ENV TRANSFORMERS_CACHE=/app/.cache/huggingface

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
