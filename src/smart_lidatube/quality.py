"""Conservative source-quality policy for audit-proposed candidates."""

LOSSLESS_CODECS = {"flac", "alac", "wav", "aiff", "ape"}


def quality_decision(current, candidate, *, edition_match=None, identity_verified=True):
    """Return ``review_only`` or ``rejected``; never auto-upgrade YouTube.

    Identity and source quality are intentionally separate gates. Container,
    extension and claimed bitrate do not establish a material improvement.
    ``edition_match`` is tri-state: only an explicit known mismatch rejects;
    FileVerifier's accepted recording match has no separate edition evidence.
    """
    if not identity_verified or edition_match is False:
        return "rejected"
    if current.get("verified") and str(current.get("codec", "")).lower() in LOSSLESS_CODECS:
        return "rejected"
    # No YouTube-derived source fact can prove quality solely from a label or
    # bitrate. A human may inspect a verified staged candidate instead.
    return "review_only"
