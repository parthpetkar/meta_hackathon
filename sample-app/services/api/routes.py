"""API route definitions for the sample service."""

from flask import Flask, jsonify


def register_routes(app: Flask) -> None:
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "healthy", "service": "api"})

    @app.route("/items", methods=["GET"])
    def list_items():
        items = [
            {"id": 1, "name": "Widget A", "price": 9.99},
            {"id": 2, "name": "Widget B", "price": 19.99},
            {"id": 3, "name": "Gadget C", "price": 29.99},
        ]
        return jsonify({"items": items, "count": len(items)})

    @app.route("/items/<int:item_id>", methods=["GET"])
    def get_item(item_id: int):
        items = {
            1: {"id": 1, "name": "Widget A", "price": 9.99},
            2: {"id": 2, "name": "Widget B", "price": 19.99},
            3: {"id": 3, "name": "Gadget C", "price": 29.99},
        }
        item = items.get(item_id)
        if item is None:
            return jsonify({"error": "Item not found"}), 404
        return jsonify(item)
