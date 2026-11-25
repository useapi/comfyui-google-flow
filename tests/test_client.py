"""Offline tests for the useapi.net client (no ComfyUI, no network). Run: python -m pytest tests"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from useapi_flow.client import Client, UseapiError, account_of, describe_error, find_token, is_media_url, media_items  # noqa: E402

REF = "user:700-email:" + "a@b.co".encode().hex() + "-video:abc"


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = "x"
        self.headers = {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw["headers"]))
        return self.answers.pop(0)


def test_account_of():
    assert account_of(REF) == "a@b.co"
    assert account_of("nope") is None


def test_media_url_allowlist():
    assert is_media_url("https://flow-content.google/video/x?sig=1")
    assert not is_media_url("http://flow-content.google/x")
    assert not is_media_url("https://evil.example/x")
    assert not is_media_url("https://flow-content.google.evil.example/x")
    assert not is_media_url("https://u@flow-content.google/x")


def test_find_token_order(tmp_path, monkeypatch):
    monkeypatch.delenv("USEAPI_TOKEN", raising=False)
    (tmp_path / "useapi_token.txt").write_text(" file-token \n")
    assert find_token(str(tmp_path)) == "file-token"
    monkeypatch.setenv("USEAPI_TOKEN", " env-token ")
    assert find_token(str(tmp_path)) == "env-token"
    monkeypatch.delenv("USEAPI_TOKEN")
    with pytest.raises(UseapiError):
        find_token(None)


def test_user_agent_and_error():
    s = FakeSession([FakeResp(401, {"error": "Unauthorized"})])
    with pytest.raises(UseapiError) as e:
        Client("t", session=s).request("GET", "accounts")
    assert e.value.status == 401
    assert s.calls[0][2]["User-Agent"].startswith("useapi-comfyui/")


def test_poll_completes_and_survives_hiccup():
    done = {"jobid": "j1", "status": "completed", "response": {"media": [{"mediaGenerationId": "m1", "videoUrl": "https://flow-content.google/v"}]}}
    s = FakeSession([FakeResp(503, {"error": "busy"}), FakeResp(200, done)])
    t = [0.0]
    job = Client("t", session=s).poll({"jobid": "j1", "status": "created"}, 100, sleep=lambda x: t.__setitem__(0, t[0] + x), clock=lambda: t[0])
    assert job["status"] == "completed"
    assert media_items(job)[0]["id"] == "m1"


def test_poll_failed_and_timeout():
    failed = {"jobid": "j1", "status": "failed", "error": "PUBLIC_ERROR_X"}
    s = FakeSession([FakeResp(200, failed)])
    with pytest.raises(UseapiError, match="failed"):
        Client("t", session=s).poll({"jobid": "j1", "status": "started"}, 100, sleep=lambda x: None)
    t = [0.0]
    s = FakeSession([FakeResp(200, {"jobid": "j1", "status": "started"})] * 5)
    with pytest.raises(UseapiError, match="still working"):
        Client("t", session=s).poll({"jobid": "j1", "status": "started"}, 15, sleep=lambda x: t.__setitem__(0, t[0] + x), clock=lambda: t[0])


def test_sync_answer_counts_as_done():
    job = Client("t", session=FakeSession([])).poll({"jobId": "j", "media": []}, 10, sleep=lambda x: None)
    assert job["status"] == "completed"


def test_describe_captcha():
    assert "free captcha credits" in describe_error(400, {"error": "No captcha provider API keys configured"})


def test_poll_stops_on_auth_error():
    s = FakeSession([FakeResp(401, {"error": "Unauthorized"})])
    with pytest.raises(UseapiError) as e:
        Client("t", session=s).poll({"jobid": "j1", "status": "started"}, 100, sleep=lambda x: None)
    assert e.value.status == 401


class StreamResp(FakeResp):
    def __init__(self, status, data, ctype="video/mp4"):
        super().__init__(status, None)
        self.data = data
        self.headers = {"Content-Type": ctype}

    def iter_content(self, chunk_size):
        yield self.data

    def close(self):
        pass


class GetSession(FakeSession):
    def get(self, url, **kw):
        self.calls.append(("GET", url, kw["headers"]))
        return self.answers.pop(0)


def test_media_download_sends_no_token_but_raw_does():
    s = GetSession([StreamResp(200, b"a"), StreamResp(200, b"b")])
    c = Client("secret", session=s)
    assert c.download("https://flow-content.google/v")[0] == b"a"
    assert "Authorization" not in s.calls[0][2]
    assert c.download_video_raw(REF)[0] == b"b"
    assert s.calls[1][1].startswith("https://api.useapi.net/v1/google-flow/assets/") and s.calls[1][1].endswith("?raw=true")
    assert s.calls[1][2]["Authorization"] == "Bearer secret"
