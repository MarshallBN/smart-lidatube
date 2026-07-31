import json
import subprocess
import pytest
from smart_lidatube.fingerprint import Fpcalc, FingerprintError, AcoustIDClient, VerificationPolicy


def test_fpcalc_safe_command_and_output():
    seen = {}
    def run(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return subprocess.CompletedProcess(args, 0, json.dumps({"duration": 201.2, "fingerprint": "fp"}), "")
    result = Fpcalc(run=run, timeout=7).calculate("odd;name.mp3")
    assert seen["args"] == ["fpcalc", "-json", "-length", "120", "odd;name.mp3"]
    assert seen["kwargs"]["shell"] is False and seen["kwargs"]["timeout"] == 7
    assert result.fingerprint == "fp"


def test_fpcalc_timeout_and_exit_code():
    with pytest.raises(FingerprintError):
        Fpcalc(run=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(a, 1))).calculate("x")
    with pytest.raises(FingerprintError):
        Fpcalc(run=lambda a, **k: subprocess.CompletedProcess(a, 2, "", "bad")).calculate("x")


def test_acoustid_request_and_policy_uncertainty():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"status":"ok", "results":[{"score":.94,"recordings":[{"id":"mb-good"}]}]}
    seen = {}
    def post(url, **kwargs): seen.update(url=url, **kwargs); return Response()
    lookup = AcoustIDClient("key", post=post).lookup("fp", 200.6)
    assert seen["data"]["duration"] == 201
    assert seen["data"]["meta"] == "recordingids sources"
    policy = VerificationPolicy(min_score=.8, duration_tolerance=8)
    assert policy.verify(lookup, "mb-good", 200, 201).verdict == "accepted"
    assert policy.verify({"results":[]}, "mb-good", 200, 201).verdict == "inconclusive"
    assert policy.verify(None, "mb-good", 200, 201, error="network").verdict == "inconclusive"
    ambiguous={"results":[{"score":.9,"recordings":[{"id":"a"}]},{"score":.89,"recordings":[{"id":"b"}]}]}
    assert policy.verify(ambiguous, "a", 200, 201).verdict == "ambiguous"
    wrong={"results":[{"score":.95,"recordings":[{"id":"other"}]}]}
    assert policy.verify(wrong, "expected", 200, 201).verdict == "rejected"
