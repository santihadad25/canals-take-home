FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY gunicorn.conf.py .
COPY app ./app

# Test image: the runtime plus pytest, the tests and the mocks they run in-process.
FROM base AS dev
COPY requirements-dev.txt pytest.ini ./
RUN pip install -r requirements-dev.txt
COPY mocks ./mocks
COPY tests ./tests

# Runtime image (default target, so it must stay last).
FROM base AS runtime
RUN useradd --system --no-create-home appuser
USER appuser
EXPOSE 8000
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app.main:create_app()"]
