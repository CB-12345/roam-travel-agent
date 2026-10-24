FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt requirements.lock.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt -c requirements.lock.txt
COPY src ./src
COPY static ./static
COPY examples ./examples
COPY main.py ./
RUN useradd --create-home roam && mkdir -p data/local-runtime && chown -R roam:roam /app
USER roam
EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/api/health', timeout=3)"
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
