"""Camera Placement relay: POST /camera-placement-service/judge.

Drives the service app through FastAPI's TestClient the way the frontend forwards a
Branch's call. OpenRouter is replaced at requests.post, so nothing leaves the process;
the tests read what would have been sent to it.

Run (from ai-camera-admin/backend):  python -m pytest tests/test_camera_placement.py -q
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys

import pytest
import requests
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

import camera_placement_service.app as cp  # noqa: E402

CODE = "ADV-TEST-0000-0001"
KEY = "sk-or-test-key"
# What OpenRouter answers, as bytes: the Branch must get these exact bytes back.
JEV_ANSWER = (b'{"model": "typesafe/jev-1.14-20261014", "answers": {"label": {"type": "choice", '
              b'"choice": "inside", "probabilities": {"inside": 1, "outside": 0, "uncertain": 0}, "confidence": 1}}, '
              b'"usage": {"input_tokens": 4839, "output_tokens": 114, "cost": 0.000203238}, "provider": "TypeSafe"}')


def jpeg(w: int, h: int, fmt: str = "JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (90, 120, 150)).save(out, fmt)
    return out.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class FakeOpenRouter:
    def __init__(self):
        self.sent = []
        self.status, self.body, self.raises = 200, JEV_ANSWER, None

    def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self.raises:
            raise self.raises
        r = requests.Response()
        r.status_code, r._content = self.status, self.body
        return r

    def picture(self) -> Image.Image:
        data = self.sent[-1]["json"]["assets"]["cam"]["data_base64"]
        return Image.open(io.BytesIO(base64.b64decode(data)))


@pytest.fixture
def openrouter(monkeypatch, tmp_path):
    fake = FakeOpenRouter()
    monkeypatch.setattr(cp.requests, "post", fake.post)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setattr(cp, "SERVER_ENV", str(tmp_path / ".env"))  # no python_server/.env unless a test writes one
    return fake


@pytest.fixture
def client():
    return TestClient(cp.app)


def judge(client, body, code=CODE):
    headers = {"X-Branch-Code": code} if code else {}
    return client.post("/judge", json=body, headers=headers)


def test_a_full_frame_is_shrunk_to_fit_and_jevs_answer_comes_back_byte_for_byte(client, openrouter):
    resp = judge(client, {"jpeg": b64(jpeg(1920, 1080)), "camera": "192.168.20.240_ch01"})
    assert resp.status_code == 200
    assert resp.content == JEV_ANSWER
    assert openrouter.picture().size == (1568, 882)
    sent = openrouter.sent[-1]
    assert sent["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    assert sent["json"]["model"] == "typesafe/jev-1.14"
    assert set(sent["json"]["questions"]) == {"label", "area"}
    assert set(sent["json"]["questions"]["label"]["criteria"]) == {"inside", "outside", "uncertain"}


def test_the_prompt_names_no_branch(client, openrouter):
    judge(client, {"jpeg": b64(jpeg(640, 480))})
    assert "UNS037" not in json.dumps(openrouter.sent[-1]["json"], ensure_ascii=False)
    assert CODE not in json.dumps(openrouter.sent[-1]["json"])


def test_a_picture_that_already_fits_goes_unchanged(client, openrouter):
    small = jpeg(352, 288)  # AVI008's NVR frame size
    judge(client, {"jpeg": b64(small)})
    assert base64.b64decode(openrouter.sent[-1]["json"]["assets"]["cam"]["data_base64"]) == small


@pytest.mark.parametrize("size, sent", [((352, 198), (498, 280)), ((200, 200), (280, 280))])
def test_a_picture_under_280_px_is_grown_to_280(client, openrouter, size, sent):
    assert judge(client, {"jpeg": b64(jpeg(*size))}).status_code == 200
    assert openrouter.picture().size == sent


def test_a_lower_max_side_sends_smaller_pictures(client, openrouter, monkeypatch):
    monkeypatch.setattr(cp, "MAX_SIDE", 498)
    judge(client, {"jpeg": b64(jpeg(1920, 1080))})
    assert openrouter.picture().size == (498, 280)


@pytest.mark.parametrize("body, why", [
    ({"jpeg": b64(jpeg(2000, 100))}, "too long and thin"),
    ({"jpeg": b64(jpeg(640, 480, "PNG"))}, "must be a JPEG"),
    ({"jpeg": b64(b"not a picture")}, "not a readable picture"),
    ({"jpeg": "%%%not base64%%%"}, "not base64"),
    ({"jpeg": b64(b"\xff" * (2 * 1024 * 1024 + 1))}, "over 2 MB"),
    ({"camera": "ch01"}, "body must be"),
    ({"jpeg": b64(jpeg(640, 480)), "camera": "x" * 65}, "at most 64"),
    (["not", "an", "object"], "body must be"),
], ids=["too thin", "png", "garbage", "not base64", "over 2 MB", "no jpeg", "long camera", "not an object"])
def test_a_bad_picture_is_400_and_never_reaches_openrouter(client, openrouter, body, why):
    resp = judge(client, body)
    assert resp.status_code == 400
    assert why in resp.json()["error"]
    assert openrouter.sent == []


def test_a_huge_declared_picture_is_refused_before_it_is_decoded(client, openrouter, monkeypatch):
    # A tiny file can claim a huge size; the cap is read from the header, so nothing is decoded.
    picture = b64(jpeg(640, 480))
    monkeypatch.setattr(cp, "MAX_PIXELS", 640 * 480 - 1)
    monkeypatch.setattr(cp.Image.Image, "load", lambda self: pytest.fail("decoded before the size check"))
    resp = judge(client, {"jpeg": picture})
    assert resp.status_code == 400 and "megapixels" in resp.json()["error"]
    assert openrouter.sent == []


def test_without_the_branch_header_it_is_400(client, openrouter):
    assert judge(client, {"jpeg": b64(jpeg(640, 480))}, code="").status_code == 400
    assert openrouter.sent == []


def test_a_body_that_is_not_json_is_400(client, openrouter):
    resp = client.post("/judge", content=b"{nope", headers={"X-Branch-Code": CODE, "Content-Type": "application/json"})
    assert resp.status_code == 400


@pytest.mark.parametrize("status, body", [
    (401, b'{"error": {"message": "API key expired.", "code": 401}}'),
    (402, b'{"error": {"message": "Insufficient credits", "code": 402}}'),
    (429, b'{"error": {"message": "rate limited", "code": 429}}'),
    (502, b'<html>bad gateway</html>'),
    (200, b'<html>not json</html>'),
], ids=["our key expired", "no credit", "rate limited", "outage", "200 without json"])
def test_anything_openrouter_cannot_answer_is_503_and_its_words_stay_here(client, openrouter, status, body):
    openrouter.status, openrouter.body = status, body
    resp = judge(client, {"jpeg": b64(jpeg(640, 480))})
    assert (resp.status_code, resp.json()) == (503, {"error": "jev_unavailable"})


def test_a_timeout_is_503(client, openrouter):
    openrouter.raises = requests.exceptions.Timeout("read timed out")
    resp = judge(client, {"jpeg": b64(jpeg(640, 480))})
    assert (resp.status_code, resp.json()) == (503, {"error": "jev_unavailable"})
    assert openrouter.sent[-1]["timeout"] < 30  # inside the frontend's forward timeout


def test_no_key_on_the_server_is_503_without_calling_openrouter(client, openrouter, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    resp = judge(client, {"jpeg": b64(jpeg(640, 480))})
    assert resp.status_code == 503
    assert openrouter.sent == []


@pytest.mark.parametrize("line", [
    "OPENROUTER_API_KEY=sk-or-from-file",
    'OPENROUTER_API_KEY="sk-or-from-file"',
    "export OPENROUTER_API_KEY='sk-or-from-file'  # ai_service",
])
def test_without_an_env_var_the_key_comes_from_python_server_env(client, openrouter, monkeypatch, line):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with open(cp.SERVER_ENV, "w") as f:
        f.write(f"DB_PASSWORD=not-this\n# OPENROUTER_API_KEY=commented-out\n{line}\n")
    assert judge(client, {"jpeg": b64(jpeg(640, 480))}).status_code == 200
    assert openrouter.sent[-1]["headers"]["Authorization"] == "Bearer sk-or-from-file"
    assert os.getenv("DB_PASSWORD") is None  # nothing else from the file leaks into the backend


def test_the_env_var_wins_over_python_server_env(client, openrouter):
    with open(cp.SERVER_ENV, "w") as f:
        f.write("OPENROUTER_API_KEY=sk-or-from-file\n")
    judge(client, {"jpeg": b64(jpeg(640, 480))})
    assert openrouter.sent[-1]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_every_call_logs_one_line_with_branch_and_cost_but_never_the_picture_or_key(client, openrouter, caplog):
    picture = b64(jpeg(640, 480))
    with caplog.at_level(logging.INFO, logger=cp.__name__):
        judge(client, {"jpeg": picture, "camera": "192.168.20.240_ch05"})
        judge(client, {"jpeg": "%%%"})
    lines = [r.getMessage() for r in caplog.records if "camera-placement branch=" in r.getMessage()]
    assert len(lines) == 2
    assert f"branch={CODE}" in lines[0] and "camera=192.168.20.240_ch05" in lines[0]
    assert "status=ok" in lines[0] and "cost=0.000203238" in lines[0]
    assert "status=bad_picture" in lines[1]
    assert picture[:40] not in caplog.text and KEY not in caplog.text
