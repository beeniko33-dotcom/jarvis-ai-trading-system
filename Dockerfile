FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JARVIS_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --create-home jarvis \
    && mkdir /data \
    && chown jarvis:jarvis /data

COPY jarvis_server ./jarvis_server
COPY frontend ./frontend

USER jarvis
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), timeout=2).close()"

CMD ["python", "jarvis_server/main_v6.py", "--host", "0.0.0.0", "--port", "8000"]
