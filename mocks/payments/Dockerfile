FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY payments_mock.py scenarios.json ./
RUN useradd --system --no-create-home mock
USER mock
EXPOSE 8000
# ONE worker with many threads: the payments live in the memory of the process, and the slow
# scenarios keep a thread busy while they sleep. The keep-alive is longer than the 2 seconds after
# which the API drops its idle connections.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--worker-class", "gthread", "--workers", "1", "--threads", "128", "--keep-alive", "5", "payments_mock:app"]
