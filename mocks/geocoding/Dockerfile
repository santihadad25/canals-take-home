FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY geocoding_mock.py addresses.json ./
RUN useradd --system --no-create-home mock
USER mock
EXPOSE 8000
# A few threads, so that many requests are served at once. The keep-alive is longer than the 2
# seconds after which the API drops its idle connections.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--worker-class", "gthread", "--workers", "1", "--threads", "32", "--keep-alive", "5", "geocoding_mock:app"]
