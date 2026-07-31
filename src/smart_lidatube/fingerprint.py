"""Chromaprint calculation and independent AcoustID lookup policy."""

import json
import subprocess
import time
from dataclasses import dataclass

import requests


class FingerprintError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fingerprint:
    duration: float
    fingerprint: str
    partial: bool = False


class Fpcalc:
    def __init__(self, executable="fpcalc", timeout=30, run=subprocess.run):
        self.executable = executable
        self.timeout = timeout
        self.run = run

    def calculate(self, path):
        args = [self.executable, "-json", "-length", "120", str(path)]
        try:
            process = self.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise FingerprintError(f"fpcalc failed: {exc}") from exc
        # fpcalc 3 means input was shorter than requested; JSON remains useful but
        # must be treated cautiously by the verifier.
        if process.returncode not in (0, 3):
            raise FingerprintError(
                f"fpcalc exit {process.returncode}: {process.stderr}"
            )
        try:
            data = json.loads(process.stdout)
            duration = float(data["duration"])
            fingerprint = data["fingerprint"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise FingerprintError("invalid fpcalc JSON") from exc
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise FingerprintError("empty fpcalc fingerprint")
        return Fingerprint(duration, fingerprint, partial=process.returncode == 3)


class AcoustIDClient:
    def __init__(
        self,
        api_key,
        post=requests.post,
        timeout=15,
        min_interval=0.34,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.api_key = api_key
        self.post = post
        self.timeout = timeout
        self.min_interval = min_interval
        self.sleep = sleep
        self.clock = clock
        self._last_request = None

    def lookup(self, fingerprint, duration):
        if not self.api_key:
            raise FingerprintError("acoustid_disabled")
        if self._last_request is not None:
            delay = self.min_interval - (self.clock() - self._last_request)
            if delay > 0:
                self.sleep(delay)
        response = self._post(fingerprint, duration)
        self._last_request = self.clock()
        if getattr(response, "status_code", None) == 429:
            try:
                retry_after = float(getattr(response, "headers", {}).get("Retry-After", 1))
            except (TypeError, ValueError):
                retry_after = 1
            self.sleep(max(retry_after, self.min_interval))
            response = self._post(fingerprint, duration)
            self._last_request = self.clock()
        response.raise_for_status()
        return response.json()

    def _post(self, fingerprint, duration):
        return self.post(
            "https://api.acoustid.org/v2/lookup",
            data={
                "client": self.api_key,
                "fingerprint": fingerprint,
                "duration": round(duration),
                "format": "json",
                "meta": "recordingids sources",
            },
            timeout=self.timeout,
        )


@dataclass(frozen=True)
class Verification:
    verdict: str
    reason: str
    evidence: dict


class VerificationPolicy:
    def __init__(self, min_score=0.8, duration_tolerance=10, ambiguity_delta=0.03):
        self.min_score = min_score
        self.duration_tolerance = duration_tolerance
        self.ambiguity_delta = ambiguity_delta

    def verify(
        self,
        lookup,
        expected_recording_id,
        expected_duration,
        actual_duration,
        error=None,
        partial=False,
    ):
        evidence = {
            "expected_recording_id": expected_recording_id,
            "expected_duration": expected_duration,
            "actual_duration": actual_duration,
            "lookup": lookup,
            "error": error,
            "partial_fingerprint": partial,
        }
        if (
            expected_duration
            and abs(actual_duration - expected_duration) > self.duration_tolerance
        ):
            return Verification("rejected", "duration_mismatch", evidence)
        if error or not lookup or not lookup.get("results"):
            return Verification("inconclusive", error or "no_match", evidence)
        results = sorted(
            lookup["results"], key=lambda item: item.get("score", 0), reverse=True
        )
        if (
            len(results) > 1
            and results[0].get("score", 0) - results[1].get("score", 0)
            < self.ambiguity_delta
        ):
            return Verification("ambiguous", "close_results", evidence)
        top = results[0]
        recording_ids = {
            recording.get("id") for recording in top.get("recordings", [])
        }
        if top.get("score", 0) < self.min_score:
            return Verification("inconclusive", "low_score", evidence)
        if expected_recording_id and expected_recording_id not in recording_ids:
            return Verification("rejected", "recording_mismatch", evidence)
        if not expected_recording_id:
            return Verification("inconclusive", "no_expected_recording", evidence)
        if partial:
            return Verification("inconclusive", "partial_fingerprint", evidence)
        return Verification("accepted", "recording_match", evidence)


class FileVerifier:
    """Combine fpcalc, AcoustID lookup and policy into a worker dependency."""

    def __init__(self, fpcalc, acoustid, policy=None):
        self.fpcalc = fpcalc
        self.acoustid = acoustid
        self.policy = policy or VerificationPolicy()

    def verify_file(self, path, identity):
        try:
            fingerprint = self.fpcalc.calculate(path)
        except FingerprintError as exc:
            return Verification(
                "inconclusive", "fingerprint_error", {"error": str(exc)}
            )
        try:
            lookup = self.acoustid.lookup(
                fingerprint.fingerprint, fingerprint.duration
            )
            error = None
        except FingerprintError as exc:
            lookup = None
            error = str(exc)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            lookup = None
            error = str(exc)
        result = self.policy.verify(
            lookup,
            identity.get("recording_id"),
            identity.get("duration"),
            fingerprint.duration,
            error=error,
            partial=fingerprint.partial,
        )
        if error == "acoustid_disabled":
            return Verification("inconclusive", "acoustid_disabled", result.evidence)
        return result
