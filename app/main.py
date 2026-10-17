import atexit

from flask import Flask

from app.adapters.api.errors import install_error_handlers
from app.adapters.api.routes import orders_blueprint
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.logging_config import configure_logging


def create_app(settings: Settings | None = None, container: Container | None = None) -> Flask:
    """Run it with `gunicorn "app.main:create_app()"` (see gunicorn.conf.py).

    A ready-made container can be passed in (the tests do): the app then does not close it.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = Flask(__name__)
    app.json.sort_keys = False  # keep the fields in the order they are written
    app.json.ensure_ascii = False  # answer with UTF-8 instead of \u00f1-style escapes

    if container is None:
        container = build_container(settings)
        atexit.register(container.close)
    app.extensions["container"] = container

    install_error_handlers(app)
    app.register_blueprint(orders_blueprint)
    return app
