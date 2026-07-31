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

The smart fork adds durable SQLite retry jobs, track-scoped YouTube rejection history, Chromaprint/AcoustID verification, Navidrome playlist ingestion, Telegram review, and token-authenticated retry/status endpoints. Copy `.env.example` and use `docker-compose.smart.example.yml` as a starting point.

### Import ownership and migration

`/lidatube/downloads` must map to a location visible to Lidarr. Replacement candidates are staged beneath `/lidatube/downloads/.smart-staging/<job>/` and submitted to Lidarr `/api/v1/manualimport` with `replaceExistingFiles=true`. **Lidarr remains the only component that names, moves, sorts, or replaces final library files.** The normal flow imports first and does not delete the current file. The explicit DELETE client method is only for operator-controlled recovery if a running Lidarr cannot replace on import. Existing missing-track behavior remains supported.

The SQLite database is created automatically at `SMART_DB_PATH`; back it up with the config volume. Navidrome `Retry` and `Manual Retry` are regular playlists. `PlaylistPoller.poll_once()` is deliberately a separate scheduler/sidecar primitive to avoid duplicate Gunicorn workers. It durably creates an idempotent job before removing a successful queue entry.

### Verification and security

The verifier invokes `fpcalc -json -length 120 FILE` without a shell. AcoustID network errors/no-match, low scores, absent expected MusicBrainz recording IDs, and ambiguity are inconclusive rather than definitely wrong. Strong duration or recording-ID mismatches are rejected.

Set a long random `SMART_API_TOKEN`. Use `Authorization: Bearer TOKEN` with `POST /api/smart/retry/<lidarr-track-id>` and `GET /api/smart/jobs/<id>`; an empty token fails closed. Telegram callbacks carry database attempt IDs only and must pass both user and chat allowlists. Keep credentials out of Git and expose APIs through HTTPS/trusted networks.

This MVP has mocked integration tests; it has not been validated against a user's live Lidarr, Navidrome, AcoustID, YouTube, or Telegram services.