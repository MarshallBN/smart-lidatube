"""Token-authenticated smart retry API."""

from functools import wraps
from uuid import uuid4

from flask import Flask, Response, jsonify, request


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
            return jsonify(store.safe_job(job_id)), 200
        return jsonify(error="not found"), 404

    def pagination():
        try:
            cursor, limit = int(request.args.get("cursor", 0)), int(request.args.get("limit", 50))
        except (TypeError, ValueError):
            return None
        return (cursor, limit) if cursor >= 0 and 1 <= limit <= 200 else None

    @app.get("/api/smart/dashboard/summary")
    @auth
    def dashboard_summary():
        return jsonify(store.dashboard_summary())

    @app.get("/api/smart/quality")
    @app.get("/api/smart/dashboard/quality")
    @auth
    def quality_summary():
        return jsonify(store.quality_summary())

    @app.get("/api/smart/events")
    @app.get("/api/smart/dashboard/events")
    @auth
    def events():
        values = pagination()
        if not values:
            return jsonify(error="cursor must be non-negative and limit must be 1-200"), 400
        cursor, limit = values; items = store.list_events(cursor, limit)
        return jsonify(items=items, next_cursor=items[-1]["id"] if items else cursor)

    @app.get("/api/smart/jobs")
    @auth
    def jobs():
        values = pagination()
        if not values:
            return jsonify(error="cursor must be non-negative and limit must be 1-200"), 400
        cursor, limit = values; items = store.list_safe_jobs(cursor, limit)
        next_cursor = int(items[-1]["id"].split(":")[1]) if items else cursor
        return jsonify(items=items, next_cursor=next_cursor)

    @app.get("/api/smart/reviews")
    @auth
    def reviews():
        values = pagination()
        if not values:
            return jsonify(error="cursor must be non-negative and limit must be 1-200"), 400
        cursor, limit = values; items = store.list_safe_reviews(cursor, limit)
        return jsonify(items=items, next_cursor=items[-1]["attempt_id"] if items else cursor)

    @app.post("/api/smart/reviews/<int:attempt_id>/action")
    @auth
    def candidate_action(attempt_id):
        action = (request.get_json(silent=True) or {}).get("action")
        if action not in {"accept", "reject", "cancel", "ignore_track", "audit_later"}:
            return jsonify(error="invalid review action"), 400
        reviews = store.list_safe_reviews(max(0, attempt_id-1), 1)
        audit_origin = reviews and reviews[0]["attempt_id"] == attempt_id and reviews[0]["audit_origin"]
        if audit_origin:
            result = store.apply_audit_review(attempt_id, action, {"api_review": True})
        elif action in {"accept", "reject", "cancel"}:
            result = store.apply_review(attempt_id, action, {"api_review": True})
        else:
            result = None
        if result is None:
            return jsonify(error="review is unavailable"), 409
        return jsonify(attempt_id=attempt_id, job_id=f"job:{result}"), 202

    @app.post("/api/smart/audit/control")
    @auth
    def audit_control():
        mode = (request.get_json(silent=True) or {}).get("mode")
        if mode not in {"observe", "review", "paused"}:
            return jsonify(error="mode must be observe, review, or paused"), 400
        store.set_setting("audit_mode", mode)
        store.record_event("api", "info", "mode_changed", "audit_mode", {"mode": mode})
        return jsonify(mode=mode), 202

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

    @app.get("/smart-control")
    def control_page():
        return Response("""<!doctype html><meta charset=utf-8><title>Smart LidaTube Control</title>
<style>body{font:16px system-ui;max-width:1100px;margin:auto;background:#111;color:#eee}section{border:1px solid #444;margin:1rem;padding:1rem}pre{white-space:pre-wrap}</style>
<h1>Smart LidaTube Control</h1><button onclick='connect()'>Connect</button>
<section><h2>Dashboard</h2><pre id=dashboard></pre></section><section><h2>Quality</h2><pre id=quality></pre></section>
<section><h2>Reviews</h2><pre id=reviews></pre></section><section><h2>Jobs</h2><pre id=jobs></pre></section>
<section><h2>Events</h2><pre id=events></pre></section><script>
let token = ''; async function load(name,path){let r=await fetch(path,{headers:{Authorization:'Bearer '+token}});document.getElementById(name).textContent=JSON.stringify(await r.json(),null,2)}
function connect(){token=prompt('API token')||'';load('dashboard','/api/smart/dashboard/summary');load('quality','/api/smart/quality');load('reviews','/api/smart/reviews');load('jobs','/api/smart/jobs');load('events','/api/smart/events')}
</script>""", mimetype="text/html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app


def create_api(store, token):
    return register_api(Flask(__name__), store, token)
