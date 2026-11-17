#!/usr/bin/env python3
"""Collect original KUKA Center KRL answers into resumable Markdown batches.

Run ``python utils/collect_krl_qa.py --dry-run`` first. A normal run prompts for
the password, login agreement consent, and the site's image CAPTCHA. Set
KUKA_PASSWORD to supply the password without putting it in source or argv.

Uses the same signed login, KRL model, streaming, answer check, and saved-answer
endpoints as the site's chat frontend. No CAPTCHA solving or browser dependency.
"""

from __future__ import annotations

import argparse
import codecs
import getpass
import hashlib
import logging
import os
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .krl_collection_store import CollectionStore, Question, parse_questions
else:
    from krl_collection_store import CollectionStore, Question, parse_questions


ROOT = Path(__file__).resolve().parents[1]
MAIN_URL = "https://www.kukacenter.com"
CHAT_URL = "https://chat.kukacenter.com"
MODEL = "krl_question_robot"  # The frontend's KRL编程 tab.
REFUSAL = "**输出答案** 非常抱歉！由于回答中可能涉及敏感词，无法提供答案。请重新输入问答，感谢您的理解和支持！"
TERMS = (
    f"{MAIN_URL}/policy/kuka_center_legal_statement.html",
    f"{MAIN_URL}/policy/kuka_center_privacy_policy.html",
    f"{MAIN_URL}/policy/kuka_bbs_policy.html",
)
LOG = logging.getLogger("collect_krl_qa")


class CollectionError(RuntimeError):
    """A failure for which the current answer must not be published."""


class LoginForm(HTMLParser):
    """Extract the CAS form's hidden flow fields, without running page scripts."""

    def __init__(self, html: str):
        super().__init__()
        self.in_form = False
        self.found = False
        self.fields: dict[str, str] = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and attributes.get("id") == "fm1":
            self.in_form = self.found = True
        elif tag == "input" and self.in_form:
            if attributes.get("type") == "hidden" and attributes.get("name"):
                self.fields[attributes["name"]] = attributes.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


@contextmanager
def output_lock(output: Path):
    """Hold an OS lock, released even after a crash, for this output series."""
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_name(f".{output.stem}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CollectionError(f"Another collector owns {output}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CollectionError(f"Another collector owns {output}") from exc
        locked = True
        yield
    finally:
        if locked and os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)
        # Keep the lock inode: unlinking permits another process to evade it.


def read_stream(response, timeout: float) -> str:
    """Decode full raw Markdown; an interrupted stream is never a success."""
    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type or "html" in content_type:
        raise CollectionError(
            "Chat returned an error/login document instead of an answer"
        )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    parts = []
    deadline = time.monotonic() + timeout
    for chunk in response.iter_content(chunk_size=4096):
        if time.monotonic() > deadline:
            raise CollectionError("Answer exceeded --answer-timeout; nothing saved")
        if chunk:
            parts.append(decoder.decode(chunk))
    parts.append(decoder.decode(b"", final=True))
    answer = "".join(parts)
    if not answer.strip():
        raise CollectionError("Chat returned an empty answer; nothing saved")
    return answer


class KukaClient:
    def __init__(self, session, request_timeout=60.0, answer_timeout=900.0):
        self.session = session
        self.request_timeout = request_timeout
        self.answer_timeout = answer_timeout

    def request(self, method, url, **kwargs):
        # requests does not automatically retry POSTs. In particular, never
        # retry an ambiguous generation and silently spend another request.
        kwargs.setdefault("timeout", (20, self.request_timeout))
        if urlsplit(url).hostname == "chat.kukacenter.com":
            kwargs.setdefault(
                "headers", {"Origin": CHAT_URL, "Referer": CHAT_URL + "/"}
            )
        try:
            response = self.session.request(method, url, **kwargs)
        except Exception as exc:
            raise CollectionError(
                f"{method} {urlsplit(url).path}: {type(exc).__name__}; request failed"
            ) from exc
        if not 200 <= response.status_code < 300:
            response.close()
            raise CollectionError(
                f"{method} {urlsplit(url).path}: HTTP {response.status_code}; stopping"
            )
        return response

    def api(self, path, payload=None, *, main_site=False):
        host = MAIN_URL if main_site else CHAT_URL
        if main_site:
            response = self.request("GET", host + path, params=payload)
        else:
            response = self.request("POST", host + path, json=payload or {})
        with response:
            try:
                data = response.json()
            except ValueError as exc:
                raise CollectionError(
                    f"{path}: expected JSON; login may have expired"
                ) from exc
        if not isinstance(data, dict) or str(data.get("code")) not in (
            "0",
            "200",
            "ok",
        ):
            # Do not echo server bodies, signed URLs, or credentials to logs.
            raise CollectionError(
                f"{path}: API rejected request; stopping without saving"
            )
        return data.get("data")

    def login(self, username, password=None, *, accept_terms=False):
        with self.request("GET", MAIN_URL + "/chat") as response:
            login_url = response.url
            form = LoginForm(response.text)
        if form.found:
            address = urlsplit(login_url)
            if (
                address.scheme != "https"
                or address.hostname != "sso.kukacenter.com"
                or address.path != "/cas/login"
            ):
                raise CollectionError(
                    "Unexpected login destination; credentials were not sent"
                )
            if not form.fields.get("execution"):
                raise CollectionError(
                    "Login flow token is missing; site interface may have changed"
                )
            if not accept_terms:
                print("KUKA Center login requires agreement to these site policies:")
                print("\n".join(TERMS))
                if (
                    input("Type yes to accept and continue login: ").strip().lower()
                    != "yes"
                ):
                    raise CollectionError("Login cancelled before sending credentials")
            if password is None:
                password = getpass.getpass("KUKA Center password: ")
            if not password:
                raise CollectionError("A password is required")
            captcha_uuid = str(uuid.uuid4())
            with tempfile.TemporaryDirectory(prefix="krl-login-") as temporary:
                captcha_path = Path(temporary) / "captcha.png"
                with self.request(
                    "GET",
                    "https://sso.kukacenter.com/cas/captcha",
                    params={"uuid": captcha_uuid},
                    headers={"Referer": login_url},
                ) as response:
                    if (
                        not response.headers.get("Content-Type", "")
                        .lower()
                        .startswith("image/")
                    ):
                        raise CollectionError(
                            "Login CAPTCHA endpoint did not return an image"
                        )
                    captcha_path.write_bytes(response.content)
                    captcha_path.chmod(0o600)
                print(
                    f"Open this image and read the site's CAPTCHA: {captcha_path}",
                    flush=True,
                )
                captcha = input("CAPTCHA (4 characters, or blank to cancel): ").strip()
                if len(captcha) != 4:
                    raise CollectionError(
                        "Login cancelled: a four-character CAPTCHA is required"
                    )
                fields = dict(form.fields)
                fields.update(
                    username=username,
                    # The site's beforeSubmit() applies md5 to the password.
                    password=hashlib.md5(
                        password.encode("utf-8"), usedforsecurity=False
                    ).hexdigest(),
                    loginType="0",
                    uuid=captcha_uuid,
                    captcha=captcha,
                    _eventId="submit",
                )
                with self.request(
                    "POST",
                    login_url,
                    data=fields,
                    headers={
                        "Origin": "https://sso.kukacenter.com",
                        "Referer": login_url,
                    },
                ) as response:
                    if (
                        LoginForm(response.text).found
                        or urlsplit(response.url).hostname == "sso.kukacenter.com"
                    ):
                        raise CollectionError(
                            "Login failed. Check credentials/CAPTCHA, then rerun"
                        )
        # The main site signs its embedded chat login; CAS cookies alone do not
        # authenticate the chat host. Each requests.Session keeps both in memory.
        signature = self.api(
            "/api-b/program_assistant/api/sign",
            {"nonceStr": str(uuid.uuid4())},
            main_site=True,
        )
        if not isinstance(signature, dict) or not all(
            signature.get(key) is not None and str(signature[key]) != ""
            for key in ("uuid", "timestamp", "nonceStr", "sign")
        ):
            raise CollectionError(
                "Signed chat login response is missing required fields"
            )
        self.api(
            "/api/login",
            {
                **{
                    key: str(signature[key])
                    for key in ("uuid", "timestamp", "nonceStr")
                },
                "signature": str(signature["sign"]),
            },
        )

    def ask(self, question: Question) -> tuple[str, dict]:
        session = self.api("/api/session/saveOrUpdate", {"sessionTitle": "新建对话"})
        if not isinstance(session, dict) or not session.get("sessionId"):
            raise CollectionError("Chat did not create a session")
        session_id = session["sessionId"]
        question_uuid = str(uuid.uuid4())
        identifiers = {
            "sessionId": session_id,
            "uuid": question_uuid,
            "requestId": session_id,
        }
        payload = dict(
            identifiers,
            question=question.prompt,
            robotModel=MODEL,
            repeatFlag=False,
            networkSearchFlag=False,
        )
        with self.request(
            "POST",
            CHAT_URL + "/api/efs/stream",
            json=payload,
            stream=True,
            timeout=(20, min(self.request_timeout, self.answer_timeout)),
        ) as response:
            streamed = read_stream(response, self.answer_timeout)
        checked = self.api("/api/check", {"content": streamed, "uuid": question_uuid})
        if not isinstance(checked, dict) or not isinstance(checked.get("status"), bool):
            raise CollectionError(
                "Chat answer check returned no boolean status; nothing saved"
            )
        metadata = {
            "source": MAIN_URL + "/chat",
            "mode": "KRL编程",
            "robot_model": MODEL,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "question_uuid": question_uuid,
            "answer_check_passed": checked["status"],
        }
        if not checked["status"]:
            # Exactly the replacement displayed by the frontend's answer check.
            return REFUSAL, metadata
        saved = self.api(
            "/api/krl/selectQuestionInfo",
            dict(identifiers, question=question.prompt, content=question.prompt),
        )
        if not isinstance(saved, dict) or not isinstance(
            saved.get("answerContent"), str
        ):
            raise CollectionError(
                "Chat did not return a persisted Markdown answer; nothing saved"
            )
        answer = saved["answerContent"]
        if not answer.strip() or answer.strip("\r\n") != streamed.strip("\r\n"):
            raise CollectionError(
                "Stream and saved answer differ; refusing a potentially incomplete answer"
            )
        metadata.update(answer_id=saved.get("id"), server_time=saved.get("time"))
        return answer, metadata


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--questions", type=Path, default=ROOT / "assets/KRL/krl_questions_1000.md"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "assets/KRL/krl_qa_collected.md"
    )
    parser.add_argument(
        "--username", default=os.environ.get("KUKA_USERNAME", "13918961550")
    )
    parser.add_argument(
        "--start", type=int, default=19, help="First sample ID, inclusive (default: 19)"
    )
    parser.add_argument(
        "--end", type=int, help="Last sample ID, inclusive (default: all)"
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--max-questions", type=int, help="Limit newly collected answers for this run"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds between questions (default: 2)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=180.0,
        help="HTTP/read idle timeout in seconds",
    )
    parser.add_argument(
        "--answer-timeout",
        type=float,
        default=900.0,
        help="Maximum streamed answer duration",
    )
    parser.add_argument(
        "--accept-login-terms",
        action="store_true",
        help="Explicitly accept the site's login policies without an interactive prompt",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate files and show pending work without network/login",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.start < 1 or (args.end is not None and args.end < args.start):
        parser.error("--start must be positive and --end must be >= --start")
    if args.batch_size < 1 or (
        args.max_questions is not None and args.max_questions < 1
    ):
        parser.error("--batch-size and --max-questions must be positive")
    if args.delay < 0 or min(args.request_timeout, args.answer_timeout) <= 0:
        parser.error("timeouts must be positive and --delay must be nonnegative")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        args.questions = args.questions.resolve()
        args.output = args.output.resolve()
        questions = parse_questions(args.questions)
        if args.start not in {question.id for question in questions}:
            raise CollectionError(f"Starting question {args.start:04d} does not exist")
        if args.questions.resolve() == args.output.resolve():
            raise CollectionError("Question source and output must be different files")
        if args.dry_run:
            store = CollectionStore(args.output, questions, args.batch_size)
            pending = store.pending(args.start, args.end)
            print(
                f"Source: {len(questions)} questions; saved: {len(store.completed_ids)}; pending: {len(pending)}"
            )
            if pending:
                print(
                    f"Next: {pending[0].id:04d} {pending[0].title}; last: {pending[-1].id:04d}"
                )
            print(f"Batch size: {args.batch_size}; output: {args.output}")
            return 0
        # Lazy import keeps --help and offline planning dependency-free.
        try:
            import requests
        except ImportError as exc:
            raise CollectionError(
                "Install requests: python -m pip install requests"
            ) from exc
        with output_lock(args.output):
            store = CollectionStore(args.output, questions, args.batch_size)
            pending = store.pending(args.start, args.end)
            if args.max_questions:
                pending = pending[: args.max_questions]
            if not pending:
                LOG.info("All selected questions are already collected")
                return 0
            LOG.info(
                "%d already saved; collecting %d pending questions from %04d",
                len(store.completed_ids),
                len(pending),
                pending[0].id,
            )
            with requests.Session() as session:
                session.headers.update(
                    {
                        "User-Agent": "ribosome-krl-collector/1.0",
                        "Referer": MAIN_URL + "/chat",
                    }
                )
                client = KukaClient(session, args.request_timeout, args.answer_timeout)
                client.login(
                    args.username,
                    os.environ.get("KUKA_PASSWORD"),
                    accept_terms=args.accept_login_terms,
                )
                for index, question in enumerate(pending):
                    LOG.info(
                        "[%d/%d] Asking %04d %s (KRL编程)",
                        index + 1,
                        len(pending),
                        question.id,
                        question.title,
                    )
                    answer, metadata = client.ask(question)
                    path = store.save(question, answer, metadata)
                    LOG.info(
                        "Saved %04d (%d characters) -> %s",
                        question.id,
                        len(answer),
                        path,
                    )
                    if index + 1 < len(pending):
                        time.sleep(args.delay)
            LOG.info(
                "Finished: %d Q&A pairs saved across output batches",
                len(store.completed_ids),
            )
        return 0
    except (KeyboardInterrupt, EOFError):
        LOG.error("Stopped. Completed pairs are preserved; rerun to resume")
        return 130
    except (CollectionError, OSError, ValueError) as exc:
        message = (
            f"{type(exc).__name__} during HTTP response"
            if type(exc).__module__.startswith(("requests.", "urllib3."))
            else str(exc)
        )
        LOG.error("%s. Completed pairs are preserved; rerun to resume", message)
        return 1
    except Exception as exc:
        # Avoid printing a requests exception that embeds a signed login URL.
        LOG.error(
            "%s during collection. Current answer was not published; completed pairs are preserved",
            type(exc).__name__,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
