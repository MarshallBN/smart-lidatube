![Build Status](https://github.com/TheWicklowWolf/LidaTube/actions/workflows/main.yml/badge.svg)
![Docker Pulls](https://img.shields.io/docker/pulls/thewicklowwolf/lidatube.svg)



<img src=src/static/lidatube.png>

LidaTube is a tool for finding and fetching missing Lidarr albums via yt-dlp.


## Run using docker-compose

```yaml
services:
  lidatube:
    image: thewicklowwolf/lidatube:latest
    container_name: lidatube
    volumes:
      - /path/to/config:/lidatube/config
      - /data/media/lidatube:/lidatube/downloads
      - /etc/localtime:/etc/localtime:ro
    ports:
      - 5000:5000
    restart: unless-stopped
```

## Configuration via environment variables

Certain values can be set via environment variables:

* __PUID__: The user ID to run the app with. Defaults to `1000`. 
* __PGID__: The group ID to run the app with. Defaults to `1000`.
* __lidarr_address__: The URL for Lidarr. Defaults to `http://192.168.1.2:8686`.
* __lidarr_api_key__: The API key for Lidarr. Defaults to ``.
* __lidarr_api_timeout__: Timeout duration for Lidarr API calls. Defaults to `120`.
* __thread_limit__: Max number of threads to use. Defaults to `1`.
* __sleep_interval__: Interval to sleep. Defaults to `0`.
* __fallback_to_top_result__: Whether to use the top result if no match is found. Defaults to `False`.
* __library_scan_on_completion__: Whether to scan Lidarr Library on completion. Defaults to `True`.
* __sync_schedule__: Schedule times to run (comma seperated values in 24hr). Defaults to ``
* __minimum_match_ratio__: Minimum percentage for a match. Defaults to `90`
* __secondary_search__: Method for secondary search (YTS or YTDLP). Defaults to `YTS`.
* __preferred_codec__: Preferred codec (mp3). Defaults to `mp3`.
* __attempt_lidarr_import__: Attempt to import each song directly into Lidarr. Defaults to `False`.


## Sync Schedule

Use a comma-separated list of hours to start sync (e.g. `2, 20` will initiate a sync at 2 AM and 8 PM).
> Note: There is a deadband of up to 10 minutes from the scheduled start time.


## Cookies (optional)
To utilize a cookies file with yt-dlp, follow these steps:

* Generate Cookies File: Open your web browser and use a suitable extension (e.g. cookies.txt for Firefox) to extract cookies for a user on YT.

* Save Cookies File: Save the obtained cookies into a file named `cookies.txt` and put it into the config folder.

---

<img src=src/static/light.png>


<img src=src/static/dark.png>


https://hub.docker.com/r/thewicklowwolf/lidatube

## Smart retry MVP

The smart fork implements a separate autonomous worker, durable SQLite jobs and attempts, track-scoped YouTube rejection history, yt-dlp/YTMusic acquisition, Chromaprint/AcoustID verification, Navidrome playlist ingestion, Telegram review, and authenticated retry/status endpoints. Copy `.env.example` and use `docker-compose.smart.example.yml`; run exactly one `smart-worker` sidecar, never one poller per Gunicorn process.

### Lifecycle and import ownership

The worker atomically claims jobs, resolves current Lidarr track identity (including MusicBrainz recording ID and duration), searches candidates, downloads into `/lidatube/downloads/.smart-staging/<job>/`, stores verification evidence, skips only rejections for the target track, and sends uncertain/manual candidates for Telegram review. Accept resumes the worker for import; reject searches the next candidate; cancel stops the job.

The downloads host volume must be visible to both containers, but its container paths may differ. Set `DOWNLOADS_ROOT` to the worker path and explicitly set `LIDARR_DOWNLOADS_ROOT` to Lidarr's path for that same volume. No Lidarr service or mount is required in this repository. Paths are canonicalized, required to remain beneath `DOWNLOADS_ROOT`, and translated before import.

For an accepted smart retry, the worker submits Lidarr's **command API**, not the interactive-import discovery endpoint:

```text
POST /api/v1/command
name: ManualImport
files[0]: { path, artistId, albumId, albumReleaseId, trackIds: [target track] }
replaceExistingFiles: true
```

`GET`/`POST /api/v1/manualimport` is Lidarr's interactive candidate-discovery/reprocessing API; it must not be used as the final one-file smart replacement action. In particular, its album-matching validation can reject a correctly selected one-file replacement as a partial album. The command payload makes the selected Lidarr track authoritative. **Lidarr alone names, moves, sorts, and replaces final library files.** Smart retry does not overwrite, delete, or pre-delete the existing file. Staging is cleaned only after a changed Lidarr track-file ID is verified.

The SQLite database is created/migrated at `SMART_DB_PATH` with WAL and busy-timeout concurrency settings; back it up with the config volume. Navidrome `Retry` and `Manual Retry` are normal playlists. Entries are durably enqueued before removal, removals run in descending index order, and the occurrence is refetched/verified before deletion.

### Verification, API, and security

The verifier invokes `fpcalc -json -length 120 FILE` without a shell. Exit 3 is retained as cautious partial evidence; empty fingerprints fail. AcoustID lookup requests `recordingids sources`, rate-limits locally, retries HTTP 429 once, and never submits fingerprints. Network/no-match, low score, absent expected ID, partial evidence, and ambiguity require review; strong duration/recording mismatches are automatically rejected.

Set a long random `SMART_API_TOKEN`. For example: `Authorization: Bearer YOUR_LONG_RANDOM_TOKEN`. Use it with `POST /api/smart/retry/<lidarr-track-id>` and `GET /api/smart/jobs/<id>`; an empty token fails closed. Modes are `auto` or `manual`. Retry POSTs are new requests by default; provide `Idempotency-Key` (or JSON `idempotency_key`) for safe replay. Telegram callbacks contain attempt IDs only, require both user and chat allowlists, are atomically one-shot, and use a SQLite-persisted update offset.

Set `NAVIDROME_MUSIC_ROOT` and `LIDARR_MUSIC_ROOT` when their library mount roots differ. Track-file lookup is paginated and ambiguous suffix matches fail closed. Claimed work uses the `SMART_CLAIM_TIMEOUT` lease, with `SMART_RETRY_DELAY` and `SMART_MAX_ATTEMPTS` controlling retry policy. Imports durably record `prepared` before the request and `submitted` only after its HTTP response. Submitted imports are checked at `SMART_IMPORT_VERIFY_INTERVAL` until `SMART_IMPORT_VERIFY_TIMEOUT`; uncertain or timed-out imports require attention and are never automatically resubmitted. Completion requires a changed associated track-file ID (or an inspectable current file when there was no prior ID), never merely a missing staged file. Rejected candidate directories are retained for diagnosis; verified imports are cleaned. Manual review requires valid Telegram configuration; failed review notifications remain durably pending and are retried by the worker.

Uppercase smart variables and lowercase legacy Lidarr variables are accepted by the sidecar. This MVP is covered by mocked behavioral/integration tests and a container health smoke test where Docker is available.

### Library integrity auditor (read-only)

The smart worker also contains a conservative library auditor. It records a separate SQLite ledger and uses Lidarr's current track-file resolution plus the existing `FileVerifier`/fpcalc/AcoustID path to classify an organized file as `verified`, `likely_correct`, `suspect`, `unverifiable`, or `unavailable`. It does **not** search YouTube, download media, create retry jobs, send remediation prompts, call a Lidarr import command, or change library files.

Set `SMART_AUDIT_ENABLED=true` to enable it (default budget: `SMART_AUDIT_VERIFY_BUDGET_PER_HOUR=12`; capped carryover: `SMART_AUDIT_MAX_TOKEN_BANK=24`). The auditor yields whenever a normal retry/import/review job exists and reserves every fifth eligible selection for the longest-unchecked track (`SMART_AUDIT_FAIRNESS_SHARE=0.20`). Candidate discovery stays disabled (`SMART_AUDIT_CANDIDATE_SEARCH_BUDGET_PER_HOUR=0`). Inspect aggregate progress through authenticated `GET /api/smart/audit/status`.

For read-only audit visibility, configure the exact Lidarr path prefix and worker mount separately: `LIDARR_MUSIC_ROOT=/Music` and `SMART_AUDIT_MUSIC_ROOT=/music`. Mount `/mnt/PizzaPool/MediaServer/Music2:/music:ro` **only** in `smart-worker`; do not add this library mount to the application service. The auditor maps only paths beneath `LIDARR_MUSIC_ROOT`, rejects traversal or unmapped paths, and records generic unavailable evidence rather than raw paths. If either mapping variable is unset, the audit fails closed and does not inspect a file.

Telegram audit summaries are report-only and deduplicated per day in SQLite. They are sent only when classifications changed (unless `SMART_AUDIT_REPORT_EMPTY=true`) and detail callbacks are paginated. Audit persistence and reports deliberately retain only safe category/reason and track display fields—never paths, URLs, raw exceptions, headers, or credentials.

### Operational recovery and runbook

The worker is deliberately fail-closed at the Lidarr boundary:

| Job condition | Meaning | Safe operator action |
|---|---|---|
| `importing` / `prepared` | The durable barrier was written before Lidarr submission. | Wait for the worker unless it transitions to `import_attention`. |
| `import_attention` + `prepared`, with no `import_result` and no `import_submitted_at` | Submission never occurred. | After correcting the cause, use the guarded retry endpoint below. It reuses the accepted staged attempt; it does not download again. |
| `import_attention` + `submitted`, or any submission timestamp/result | Submission outcome is uncertain or needs inspection. | **Do not resubmit automatically.** Inspect Lidarr and create a new explicit retry if appropriate. |
| `completed` | The target Lidarr track-file association changed. | Verify playback; normal staging cleanup may proceed. |

```bash
# Inspect a job (token is required)
curl -H "Authorization: Bearer $SMART_API_TOKEN" \
  http://SMART_LIDATUBE_HOST:5000/api/smart/jobs/JOB_ID

# Only for an unsubmitted prepared import_attention job
curl -X POST -H "Authorization: Bearer $SMART_API_TOKEN" \
  http://SMART_LIDATUBE_HOST:5000/api/smart/jobs/JOB_ID/retry-import
```

Do not store real API tokens, passwords, bot tokens, or connection strings in the repository. Rotate any credential exposed in chat or logs and update the deployment's secret/environment configuration. The main smart workflow has been live-validated with Navidrome playlist ingestion, Telegram approval, and a Lidarr `ManualImport` command; external APIs and Lidarr versions can still require future compatibility adjustments.