"""Compatibility no-op for retired audit remediation dispatch."""


class RemediationDispatcher:
    """Preserve the runner seam without creating audit-origin jobs."""

    def __init__(self, store, budget_per_hour=0, max_token_bank=2, clock=None):
        self.store = store

    def dispatch_once(self):
        """Audit findings are report-only; never create remediation jobs."""
        return None
