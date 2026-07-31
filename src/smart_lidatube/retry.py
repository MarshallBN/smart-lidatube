"""Retry paths and safe Navidrome playlist ingestion."""

from pathlib import Path


def staging_path(downloads_root, job_id, filename):
    root = Path(downloads_root) / ".smart-staging" / str(job_id)
    root.mkdir(parents=True, exist_ok=True)
    return root / Path(filename).name


def filter_candidates(store, track_id, candidates):
    return [
        candidate
        for candidate in candidates
        if not store.is_rejected(
            track_id, candidate["provider"], candidate["source_id"]
        )
    ]


class PlaylistPoller:
    """Single-cycle, durable ingestion with index-safe playlist removal."""

    def __init__(self, navidrome, store, resolve_track):
        self.navidrome = navidrome
        self.store = store
        self.resolve_track = resolve_track

    def poll_once(self):
        queued = []
        entries = self.navidrome.retry_entries()
        # Removing from highest index first prevents lower indices shifting.
        entries.sort(
            key=lambda item: (str(item["playlist_id"]), item["playlist_index"]),
            reverse=True,
        )
        for entry in entries:
            track_id = self.resolve_track(entry)
            if track_id is None:
                continue
            # Include index and path: duplicate song IDs/occurrences are distinct,
            # while an unchanged occurrence remains idempotent after a crash.
            key = (
                f"navidrome:{entry['playlist_id']}:{entry['playlist_index']}:"
                f"{entry.get('id')}:{entry.get('path', '')}"
            )
            job = self.store.enqueue_job(
                track_id,
                key,
                "manual" if entry["playlist_name"] == "Manual Retry" else "auto",
                entry,
            )
            # enqueue_job commits before this call. Client refetch/verification
            # ensures a concurrent playlist edit does not remove another song.
            try:
                removed = self.navidrome.remove_entry(
                    entry["playlist_id"],
                    entry["playlist_index"],
                    expected_id=entry.get("id"),
                )
            except TypeError:
                # Compatibility with existing injected/simple clients.
                removed = self.navidrome.remove_entry(
                    entry["playlist_id"], entry["playlist_index"]
                )
            if removed is not False:
                queued.append(job)
        return queued
