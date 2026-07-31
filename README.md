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

The downloads mount must be visible to Lidarr at the same configured path. Accepted files are submitted to Lidarr `/api/v1/manualimport` with `replaceExistingFiles=true`. **Lidarr alone names, moves, sorts, and replaces final library files.** Smart retry does not overwrite or pre-delete the existing file. Existing missing-track UI behavior remains supported.

The SQLite database is created/migrated at `SMART_DB_PATH` with WAL and busy-timeout concurrency settings; back it up with the config volume. Navidrome `Retry` and `Manual Retry` are normal playlists. Entries are durably enqueued before removal, removals run in descending index order, and the occurrence is refetched/verified before deletion.

### Verification, API, and security

The verifier invokes `fpcalc -json -length 120 FILE` without a shell. Exit 3 is retained as cautious partial evidence; empty fingerprints fail. AcoustID lookup requests `recordingids sources`, rate-limits locally, retries HTTP 429 once, and never submits fingerprints. Network/no-match, low score, absent expected ID, partial evidence, and ambiguity require review; strong duration/recording mismatches are automatically rejected.

Set a long random `SMART_API_TOKEN`. Use the header **Authorization: Bearer TOKEN** with `POST /api/smart/retry/<lidarr-track-id>` and `GET /api/smart/jobs/<id>`; an empty token fails closed. Modes are `auto` or `manual`. Retry POSTs are new requests by default; provide `Idempotency-Key` (or JSON `idempotency_key`) for safe replay. Telegram callbacks contain attempt IDs only and require both user and chat allowlists.

Uppercase smart variables and lowercase legacy Lidarr variables are accepted by the sidecar. This MVP is covered by mocked behavioral/integration tests and a container health smoke test where Docker is available. It has **not** been validated against live Lidarr, Navidrome, AcoustID, YouTube, or Telegram services; API payload/version differences may require field adaptation.