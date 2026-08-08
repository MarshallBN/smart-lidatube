"""Canonical audit-remediation marker handling."""

AUDIT_ORIGIN_SQL = "(json_type(j.metadata, '$.audit_remediation') = 'true' OR json_type(j.metadata, '$.audit_remediation') = 'object')"


def is_audit_origin(job):
    """Return whether a decoded job has a canonical audit origin marker."""
    marker = (job or {}).get("metadata", {}).get("audit_remediation")
    return marker is True or isinstance(marker, dict)
