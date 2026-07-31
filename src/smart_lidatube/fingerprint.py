import json
import subprocess
from dataclasses import dataclass
import requests


class FingerprintError(RuntimeError): pass


@dataclass(frozen=True)
class Fingerprint:
    duration: float
    fingerprint: str


class Fpcalc:
    def __init__(self, executable="fpcalc", timeout=30, run=subprocess.run):
        self.executable, self.timeout, self.run = executable, timeout, run

    def calculate(self, path):
        args=[self.executable,"-json","-length","120",str(path)]
        try:
            proc=self.run(args,capture_output=True,text=True,timeout=self.timeout,shell=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise FingerprintError(f"fpcalc failed: {exc}") from exc
        if proc.returncode:
            raise FingerprintError(f"fpcalc exit {proc.returncode}: {proc.stderr}")
        try:
            data=json.loads(proc.stdout); return Fingerprint(float(data["duration"]),data["fingerprint"])
        except (ValueError,KeyError,TypeError) as exc: raise FingerprintError("invalid fpcalc JSON") from exc


class AcoustIDClient:
    def __init__(self, api_key, post=requests.post, timeout=15): self.api_key,self.post,self.timeout=api_key,post,timeout
    def lookup(self, fingerprint, duration):
        response=self.post("https://api.acoustid.org/v2/lookup",data={"client":self.api_key,"fingerprint":fingerprint,"duration":round(duration),"format":"json","meta":"recordingids"},timeout=self.timeout)
        response.raise_for_status(); return response.json()


@dataclass(frozen=True)
class Verification:
    verdict: str
    reason: str
    evidence: dict


class VerificationPolicy:
    def __init__(self,min_score=.8,duration_tolerance=10,ambiguity_delta=.03): self.min_score,self.duration_tolerance,self.ambiguity_delta=min_score,duration_tolerance,ambiguity_delta
    def verify(self,lookup,expected_recording_id,expected_duration,actual_duration,error=None):
        evidence={"expected_recording_id":expected_recording_id,"expected_duration":expected_duration,"actual_duration":actual_duration,"lookup":lookup,"error":error}
        if expected_duration and abs(actual_duration-expected_duration)>self.duration_tolerance: return Verification("rejected","duration_mismatch",evidence)
        if error or not lookup or not lookup.get("results"): return Verification("inconclusive",error or "no_match",evidence)
        results=sorted(lookup["results"],key=lambda x:x.get("score",0),reverse=True)
        if len(results)>1 and results[0].get("score",0)-results[1].get("score",0)<self.ambiguity_delta: return Verification("ambiguous","close_results",evidence)
        top=results[0]; ids={r.get("id") for r in top.get("recordings",[])}
        if top.get("score",0)<self.min_score: return Verification("inconclusive","low_score",evidence)
        if expected_recording_id and expected_recording_id not in ids: return Verification("rejected","recording_mismatch",evidence)
        if not expected_recording_id: return Verification("inconclusive","no_expected_recording",evidence)
        return Verification("accepted","recording_match",evidence)
