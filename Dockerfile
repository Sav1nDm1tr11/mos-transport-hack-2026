FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY services services
COPY packages packages
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home app && mkdir -p /data && chown app:app /data
USER app
ENV TRANSPORT_DB_PATH=/data/transport.sqlite TRANSPORT_NDTP_HOST=0.0.0.0
EXPOSE 8000 9201
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=2)"
CMD ["python", "-m", "uvicorn", "backend.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
