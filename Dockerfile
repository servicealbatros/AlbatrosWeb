FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ALBATROS_DATA_DIR=/data/albatros_ai \
    ROBOT_ENABLED=false BETA_MODE=true

WORKDIR /app
RUN useradd --create-home --uid 10001 albatros && mkdir -p /data/albatros_ai && chown -R albatros:albatros /app /data/albatros_ai
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER albatros
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

