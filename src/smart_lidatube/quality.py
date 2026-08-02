"""Conservative source-quality policy for audit-proposed candidates."""

LOSSLESS_CODECS = {"flac", "alac", "wav", "aiff", "ape"}


def quality_decision(current, candidate, *, edition_match=True, identity_verified=True):
    """Return ``review_only`` or ``rejected``; never auto-upgrade YouTube.

    Identity and source quality are intentionally separate gates.  Container,
    extension and claimed bitrate do not establish a material improvement.
    """
    if not identity_verified or not edition_match:
        return "rejected"
    if current.get("verified") and str(current.get("codec", "")).lower() in LOSSLESS_CODECS:
        return "rejected"
    # No YouTube-derived source fact can prove quality solely from a label or
    # bitrate. A human may inspect a verified staged candidate instead.
    return "review_only"
