"""Gunicorn settings for the API: several worker processes, each one with its own threads.

A request spends nearly all its time waiting (the database, the geocoder, the payment provider),
so threads give each process a lot of concurrency. The processes share nothing but PostgreSQL.
"""

import os

bind = "0.0.0.0:8000"

worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "4"))
threads = int(os.environ.get("GUNICORN_THREADS", "16"))

# Every worker builds its own application (and its own connection pool) after it is forked.
preload_app = False

# The worker's heartbeat does not depend on the requests, so a slow request never gets it killed.
timeout = 60
graceful_timeout = 30
keepalive = 5

accesslog = "-"
