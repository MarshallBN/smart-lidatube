"""Low-rate, review-gated dispatch of audit remediation work."""
from datetime import datetime, timezone
from uuid import uuid4


class RemediationDispatcher:
    """Turn eligible audit findings into existing manual review jobs only.

    This component never searches, downloads, imports, or edits the library.
    The normal JobWorker retains the existing staged verification workflow.
    """

    def __init__(self, store, budget_per_hour=0, max_token_bank=2, clock=None):
        self.store = store
        self.budget_per_hour = max(0, int(budget_per_hour))
        self.max_token_bank = max(1, int(max_token_bank))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _token(self):
        if self.budget_per_hour <= 0:
            return False
        now = self.clock().timestamp()
        raw = self.store.get_setting("remediation_search_tokens")
        tokens, updated = (self.max_token_bank, now) if not raw else map(float, raw.split(":"))
        tokens = min(self.max_token_bank, tokens + (now - updated) * self.budget_per_hour / 3600)
        if tokens < 1:
            self.store.set_setting("remediation_search_tokens", f"{tokens}:{now}")
            return False
        self.store.set_setting("remediation_search_tokens", f"{tokens - 1}:{now}")
        return True

    def dispatch_once(self):
        if self.store.regular_work_pending():
            return None
        item = self.store.claim_remediation()
        if not item:
            return None
        if not self._token():
            self.store.release_remediation(item["id"])
            return None
        job_id = self.store.create_remediation_job(
            item,
            f"audit-remediation:{item['lidarr_track_id']}:{uuid4()}",
            {"audit_remediation": item["reason"]},
        )
        return job_id
