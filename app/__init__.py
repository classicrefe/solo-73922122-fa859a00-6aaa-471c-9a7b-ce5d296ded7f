"""Flask application factory."""
from __future__ import annotations

import os

from flask import Flask

from .db import active_version, close_db, get_db, init_db


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    db_path = os.environ.get("DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "catalog.db",
    )
    app.config.from_mapping(
        DATABASE=db_path,
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        JSON_AS_ASCII=False,
    )
    if test_config:
        app.config.update(test_config)

    app.json.ensure_ascii = False

    @app.template_filter("from_json")
    def _from_json(value):
        import json

        try:
            return json.loads(value or "[]")
        except (TypeError, ValueError):
            return []

    @app.context_processor
    def _inject_active_version():
        try:
            return {"active_version": active_version(get_db())}
        except Exception:  # pragma: no cover - rendering must not fail on nav
            return {}

    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    from .routes import bp

    app.register_blueprint(bp)
    return app
