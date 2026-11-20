"""Offline OCR/CLI contracts; no account or external service required."""

from io import BytesIO
from types import SimpleNamespace

import pytest
import requests

from utils.krl import solve_captcha as solver


def test_url_cli_prints_only_four_characters_and_reads_downloaded_bytes(monkeypatch, capsys):
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = "image/png"
    response.raw = BytesIO(b"downloaded-captcha")
    calls = []

    def download(url, **kwargs):
        calls.append((url, kwargs))
        return response

    def recognize(image, **kwargs):
        assert image == b"downloaded-captcha"
        print("library diagnostic")
        return " aB27\n"

    monkeypatch.setattr(requests, "get", download)
    monkeypatch.setattr(solver, "_ocr_engine", lambda: SimpleNamespace(classification=recognize))
    assert solver.main(["https://example.test/captcha?uuid=private-token"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "aB27\n"
    assert "library diagnostic" in captured.err
    assert len(calls) == 1 and calls[0][1]["timeout"] == (20, 30.0)


@pytest.mark.parametrize("result", ["", "abc", "abcde", "ab 2", "ＡB27", "ab!?", None])
def test_ocr_rejects_invalid_codes_without_stdout(result, monkeypatch, capsys, tmp_path):
    image = tmp_path / "captcha.png"
    image.write_bytes(b"captcha")
    monkeypatch.setattr(solver, "_ocr_engine", lambda: SimpleNamespace(classification=lambda *a, **k: result))
    assert solver.main([str(image)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exactly four" in captured.err


def test_file_url_with_spaces_reads_the_existing_image(tmp_path):
    image = tmp_path / "captcha image.png"
    image.write_bytes(b"existing-captcha")
    assert solver.read_image(image.as_uri()) == b"existing-captcha"


def test_download_failure_does_not_echo_private_url(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("https://example.test/captcha?secret=private-token")

    monkeypatch.setattr(requests, "get", fail)
    assert solver.main(["https://example.test/captcha?secret=private-token"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-token" not in captured.err


def test_html_response_is_rejected_before_ocr(monkeypatch):
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = "text/html"
    response.raw = BytesIO(b"<html>login required</html>")
    monkeypatch.setattr(requests, "get", lambda *a, **k: response)
    with pytest.raises(solver.CaptchaError, match="did not return an image"):
        solver.read_image("https://example.test/captcha")
