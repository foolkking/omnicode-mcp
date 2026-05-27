FROM python:3.11-slim

WORKDIR /app

# System deps for tree-sitter compilation + git
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir -e .

# Pre-download the embedding model so startup is instant
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy source
COPY . .

# Environment
ENV TRANSFORMERS_OFFLINE=1
ENV HF_HUB_OFFLINE=1
ENV HF_DATASETS_OFFLINE=1
ENV API_HOST=0.0.0.0
ENV API_PORT=6789

EXPOSE 6789

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -f http://127.0.0.1:6789/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "6789"]
