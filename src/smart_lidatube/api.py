"""Token-authenticated smart retry API."""

from functools import wraps
from uuid import uuid4

from flask import Flask, jsonify, request


VALID_MODES = {"auto", "manual"}


def register_api(app, store, token):
    def auth(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if not token or request.headers.get("Authorization") != f"Bearer {token}":
                return jsonify(error="unauthorized"), 401
            return function(*args, **kwargs)

        return wrapped

    @app.post("/api/smart/retry/<int:track_id>")
    @auth
    def retry(track_id):
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "auto")
        if mode not in VALID_MODES:
            return jsonify(error="mode must be 'auto' or 'manual'"), 400
        supplied_key = request.headers.get("Idempotency-Key") or body.get(
            "idempotency_key"
        )
        # No key means a deliberate new retry. A caller-supplied key enables safe
        # replay and is namespaced to API requests.
        key = f"api:{supplied_key or uuid4()}"
        job = store.enqueue_job(track_id, key, mode)
        return jsonify(job_id=job, idempotency_key=key), 202

    @app.get("/api/smart/jobs/<int:job_id>")
    @auth
    def job(job_id):
        value = store.get_job(job_id)
        if value:
            return jsonify(value), 200
        return jsonify(error="not found"), 404

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app


def create_api(store, token):
    return register_api(Flask(__name__), store, token)
