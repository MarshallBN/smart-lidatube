"""Fail-closed mapping from Lidarr library paths to the audit-only mount."""
from pathlib import Path, PurePosixPath


def map_lidarr_music_path(path, lidarr_music_root, audit_music_root):
    """Map one absolute Lidarr path beneath its configured music root."""
    if not path or not lidarr_music_root or not audit_music_root:
        return None
    source = PurePosixPath(path)
    lidarr_root = PurePosixPath(lidarr_music_root)
    audit_root = Path(audit_music_root)
    if not source.is_absolute() or not lidarr_root.is_absolute() or not audit_root.is_absolute():
        return None
    try:
        relative = source.relative_to(lidarr_root)
    except ValueError:
        return None
    if ".." in relative.parts:
        return None
    return audit_root.joinpath(*relative.parts)
