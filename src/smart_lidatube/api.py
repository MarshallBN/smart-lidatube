from functools import wraps
from flask import Flask,request,jsonify


def create_api(store,token):
    app=Flask(__name__)
    def auth(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if not token or request.headers.get("Authorization") != f"Bearer {token}": return jsonify(error="unauthorized"),401
            return fn(*args,**kwargs)
        return wrapped
    @app.post("/api/smart/retry/<int:track_id>")
    @auth
    def retry(track_id):
        mode=(request.get_json(silent=True) or {}).get("mode","auto")
        job=store.enqueue_job(track_id,f"api:{track_id}:{mode}",mode); return jsonify(job_id=job),202
    @app.get("/api/smart/jobs/<int:job_id>")
    @auth
    def job(job_id):
        value=store.get_job(job_id)
        return (jsonify(value),200) if value else (jsonify(error="not found"),404)
    @app.get("/health")
    def health(): return jsonify(status="ok")
    return app
