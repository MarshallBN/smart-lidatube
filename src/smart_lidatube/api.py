"""Token-authenticated smart retry API."""

from functools import wraps
from uuid import uuid4

from flask import Flask, jsonify, request


VALID_MODES = {"auto", "manual"}
SAFE_AUDIT_REQUEUE_STATUSES = {"unavailable", "unverifiable"}
MAX_AUDIT_REQUEUE_LIMIT = 200


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

    @app.post("/api/smart/jobs/<int:job_id>/retry-import")
    @auth
    def retry_import(job_id):
        if store.retry_prepared_import(job_id):
            return jsonify(job_id=job_id, status="ready_import"), 202
        if not store.get_job(job_id):
            return jsonify(error="not found"), 404
        return jsonify(error="only an unsubmitted prepared import in import_attention may be retried"), 409

    @app.get("/api/smart/jobs/<int:job_id>")
    @auth
    def job(job_id):
        value = store.get_job(job_id)
        if value:
            return jsonify(value), 200
        return jsonify(error="not found"), 404

    @app.get("/api/smart/audit/status")
    @auth
    def audit_status():
        return jsonify(audit=store.audit_status())

    @app.post("/api/smart/audit/requeue")
    @auth
    def requeue_audits():
        body = request.get_json(silent=True) or {}
        statuses = body.get("statuses", sorted(SAFE_AUDIT_REQUEUE_STATUSES))
        limit = body.get("limit", MAX_AUDIT_REQUEUE_LIMIT)
        if (
            not isinstance(statuses, list)
            or not statuses
            or any(not isinstance(status, str) for status in statuses)
            or not set(statuses).issubset(SAFE_AUDIT_REQUEUE_STATUSES)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_AUDIT_REQUEUE_LIMIT
        ):
            return jsonify(error="statuses must be unavailable/unverifiable and limit must be 1-200"), 400
        return jsonify(requeued=store.requeue_audits(sorted(set(statuses)), limit)), 202

    @app.post("/api/smart/audit/attempts/<int:attempt_id>/review")
    @auth
    def review_audit_attempt(attempt_id):
        action = (request.get_json(silent=True) or {}).get("action")
        statuses = {
            "accept": "ready_import",
            "reject": "queued",
            "ignore_track": "cancelled",
            "audit_later": "queued",
        }
        if action not in statuses:
            return jsonify(error="invalid audit review action"), 400
        if store.apply_audit_review(attempt_id, action, {"api_review": True}) is None:
            return jsonify(error="audit review is unavailable"), 409
        return jsonify(attempt_id=attempt_id, status=statuses[action]), 202

    @app.post("/api/smart/audit/<int:track_id>/ignore")
    @auth
    def ignore_audit_track(track_id):
        # Reversible, track-scoped deferral. It cannot import or mutate media.
        store.set_audit_exemption(track_id, do_not_upgrade=True)
        return jsonify(track_id=track_id, do_not_upgrade=True), 202

    @app.post("/api/smart/audit/<int:track_id>/later")
    @auth
    def audit_later(track_id):
        store.set_audit_exemption(track_id, do_not_upgrade=False)
        return jsonify(track_id=track_id, do_not_upgrade=False), 202

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app


def create_api(store, token):
    return register_api(Flask(__name__), store, token)
