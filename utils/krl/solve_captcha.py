#!/usr/bin/env python3
"""Read four CAPTCHA characters from an image URL or local path.

Install the optional local OCR engine with ``python -m pip install ddddocr``.
Successful CLI runs print only the four characters and a newline to stdout.
The collectors use ``solve_captcha`` directly to preserve the login session
and the UUID associated with the downloaded challenge.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from contextlib import redirect_stdout
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

MAX_IMAGE_BYTES = 8 * 1024 * 1024


class CaptchaError(RuntimeError):
    """A CAPTCHA could not be downloaded or read as four characters."""


@lru_cache(maxsize=1)
def _ocr_engine():
    try:
        # Keep library diagnostics off stdout so the CLI can be piped.
        with redirect_stdout(sys.stderr):
            import ddddocr

            return ddddocr.DdddOcr(show_ad=False)
    except ImportError as exc:
        raise CaptchaError(
            "Install CAPTCHA OCR in this Python environment: "
            "python -m pip install 'ddddocr>=1.6,<2'"
        ) from exc
    except Exception as exc:
        raise CaptchaError("Could not initialize the local CAPTCHA OCR engine") from exc


def solve_captcha(image: bytes) -> str:
    """Recognize a four-character alphanumeric CAPTCHA locally, preserving case."""
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise CaptchaError("CAPTCHA image must be nonempty and at most 8 MiB")
    engine = _ocr_engine()
    try:
        with redirect_stdout(sys.stderr):
            result = engine.classification(image, png_fix=True)
    except Exception as exc:
        raise CaptchaError("Could not read the CAPTCHA image") from exc
    code = result.strip() if isinstance(result, str) else ""
    if not re.fullmatch(r"[A-Za-z0-9]{4}", code):
        raise CaptchaError("OCR did not return exactly four letters/digits; rerun for a fresh CAPTCHA")
    return code


def read_image(source: str, *, timeout: float = 30.0) -> bytes:
    """Download a URL once, or read a local image, without persisting a copy."""
    address = urlsplit(source)
    if address.scheme in ("http", "https"):
        try:
            import requests
        except ImportError as exc:
            raise CaptchaError("Install URL support: python -m pip install requests") from exc
        try:
            with requests.get(source, timeout=(20, timeout), stream=True) as response:
                response.raise_for_status()
                if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                    raise CaptchaError("CAPTCHA URL did not return an image")
                image = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    image.extend(chunk)
                    if len(image) > MAX_IMAGE_BYTES:
                        raise CaptchaError("CAPTCHA image exceeds 8 MiB")
                return bytes(image)
        except requests.RequestException as exc:
            # A challenge URL can contain private tokens; do not echo it.
            raise CaptchaError("Could not download the CAPTCHA image") from exc
    if address.scheme == "file":
        if address.netloc not in ("", "localhost"):
            raise CaptchaError("A file URL must refer to a local image")
        source = url2pathname(address.path)
    try:
        return Path(source).expanduser().read_bytes()
    except OSError as exc:
        raise CaptchaError("Could not read the local CAPTCHA image") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="HTTP(S) image URL, local path, or file:// URL")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP read timeout in seconds (default: 30)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    try:
        code = solve_captcha(read_image(args.image, timeout=args.timeout))
    except CaptchaError as exc:
        print(f"CAPTCHA error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
