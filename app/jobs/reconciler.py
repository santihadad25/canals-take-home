"""Runs ReconcileOrders over and over: `python -m app.jobs.reconciler`."""

import logging
import time

from app.config import get_settings
from app.container import Container, build_container
from app.logging_config import configure_logging

logger = logging.getLogger("reconciler")


def run_reconciler() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    container = build_container(settings)

    logger.info("started, running every %ss", settings.reconciler_interval_seconds)
    while True:
        _reconcile_once(container)
        time.sleep(settings.reconciler_interval_seconds)


def _reconcile_once(container: Container) -> None:
    try:
        report = container.reconcile_orders.run_once()
    except Exception:
        logger.exception("run failed")  # the next run tries again
        return
    if report.authorized_orders_processed or report.orders_expired or report.failures:
        logger.info("run finished: %s", report)


if __name__ == "__main__":
    run_reconciler()
