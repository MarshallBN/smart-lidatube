from pathlib import Path


def staging_path(downloads_root,job_id,filename):
    root=Path(downloads_root)/".smart-staging"/str(job_id); root.mkdir(parents=True,exist_ok=True)
    return root/Path(filename).name


def filter_candidates(store,track_id,candidates):
    return [c for c in candidates if not store.is_rejected(track_id,c["provider"],c["source_id"])]


class PlaylistPoller:
    """Single-cycle poller; run from cron/separate process to avoid Gunicorn duplication."""
    def __init__(self,navidrome,store,resolve_track): self.navidrome,self.store,self.resolve_track=navidrome,store,resolve_track
    def poll_once(self):
        queued=[]
        for entry in self.navidrome.retry_entries():
            track_id=self.resolve_track(entry)
            if track_id is None: continue
            key=f"navidrome:{entry['playlist_id']}:{entry['id']}"
            job=self.store.enqueue_job(track_id,key,"manual" if entry["playlist_name"]=="Manual Retry" else "auto",entry)
            self.navidrome.remove_entry(entry["playlist_id"],entry["playlist_index"]); queued.append(job)
        return queued
