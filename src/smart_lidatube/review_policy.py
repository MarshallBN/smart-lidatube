"""Shared policy for actions on provider review candidates."""


def review_action_error(provider, action):
    """Return a public-safe rejection reason, or ``None`` when allowed."""
    if action == "accept" and provider == "slskd":
        return "slskd acquisition is not enabled"
    return None
