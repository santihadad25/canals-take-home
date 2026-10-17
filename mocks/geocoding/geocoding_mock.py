"""Mock geocoding service. Which addresses it knows is decided by addresses.json.

GET /geocode?address=...  ->  200 {"lat": ..., "lon": ...}
                              404 when the address is unknown
                              503 for the addresses listed under "unavailable"
"""

import json
import os
from pathlib import Path

from flask import Flask, jsonify, request


def _normalize(address: str) -> str:
    return " ".join(address.split()).casefold()


def create_app(config_path: str | None = None) -> Flask:
    path = Path(config_path or os.environ.get("ADDRESSES_FILE", "addresses.json"))
    config = json.loads(path.read_text(encoding="utf-8"))
    known = {_normalize(address): coords for address, coords in config["addresses"].items()}
    unavailable = {_normalize(address) for address in config.get("unavailable", [])}

    app = Flask(__name__)

    @app.get("/geocode")
    def geocode():
        address = request.args.get("address")
        if address is None:
            return jsonify(
                {"error": {"code": "address_required", "message": "send an address"}}
            ), 422
        key = _normalize(address)
        if key in unavailable:
            return jsonify(
                {"error": {"code": "service_unavailable", "message": "try again later"}}
            ), 503
        coords = known.get(key)
        if coords is None:
            return jsonify({"error": {"code": "address_not_found", "message": "no results"}}), 404
        return jsonify({"lat": coords["lat"], "lon": coords["lon"]})

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


app = create_app()
