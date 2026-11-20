#!/usr/bin/env python3
"""Collect KUKA Center KRL程序理解 answers into resumable Markdown batches.

Run ``python utils/krl/collect_krlu_qa.py --dry-run`` first. Each source question
contains a generated KRL sample program. This entry point shares login,
empty-conversation recovery, streaming validation, retries, concurrency, and
atomic storage with the existing collectors. Credentials come from utils/krl/.env
and --auto-captcha reads and submits the site's image CAPTCHA locally.
Without that option, enter the four characters at the prompt.
"""

from __future__ import annotations

import re
import sys
import time
import uuid

if __package__:
    from . import collect_krl_qa as collector
else:
    import collect_krl_qa as collector


PROFILE = collector.CollectionProfile(
    key="krlu",
    mode="KRL程序理解",
    # The project endpoint selects this mode; its payload has no robotModel.
    robot_model=None,
    questions=collector.ROOT / "assets/KRLU/krlu_questions_1000.md",
    output=collector.ROOT / "assets/KRLU/krlu_qa_collected.md",
    start=1,
    title="KRLU程序理解问答采集",
    answer_path="/api/krl/selectQuestionInfo",
    stream_path="/api/krl/stream",
)


def program_files(question):
    """Extract marked KRL fences as complete, safe SRC/DAT upload files."""
    files = []
    names = set()
    fence = None
    body = []
    is_krl = False
    for line in question.prompt.splitlines(keepends=True):
        stripped = line.strip()
        if fence is None:
            opening = re.fullmatch(r"(`{3,}|~{3,})([^`~]*)", stripped)
            if opening:
                fence = opening[1]
                is_krl = opening[2].strip().lower() == "krl"
                body = []
        elif re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}", stripped):
            if is_krl:
                source = "".join(body)
                marker = re.fullmatch(
                    r";[ \t]*FILE:[ \t]*([A-Za-z][A-Za-z0-9_]{0,23}\.(?:src|dat))[ \t]*",
                    body[0].strip() if body else "", re.IGNORECASE,
                )
                if not marker or len(body) < 2 or not "".join(body[1:]).strip():
                    raise collector.CollectionError(
                        f"Question {question.id:04d}: each KRL fence needs a safe '; FILE: name.src/dat' marker and program body"
                    )
                name = marker[1]
                if name.lower() in names:
                    raise collector.CollectionError(
                        f"Question {question.id:04d}: duplicate program filename"
                    )
                names.add(name.lower())
                files.append((name, source.encode("utf-8")))
            fence = None
        elif is_krl:
            body.append(line)
    if fence is not None:
        raise collector.CollectionError(f"Question {question.id:04d}: unclosed code fence")
    if not files or len(files) > 30 or not any(name.lower().endswith(".src") for name, _ in files):
        raise collector.CollectionError(
            f"Question {question.id:04d}: expected 1–30 marked KRL files including a .src program"
        )
    return files


def _training_status(data):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise collector.CollectionError("Project training returned an invalid status object")
    value = data.get("status")
    if value is None:
        return None
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise collector.CollectionError("Project training returned an invalid status value")


class KrluClient(collector.KukaClient):
    """Train each question's generated project before asking for understanding."""

    def __init__(self, *args, profile=PROFILE, **kwargs):
        super().__init__(*args, profile=profile, **kwargs)

    @staticmethod
    def validate_question(question):
        program_files(question)

    def _latest_empty_conversation(self):
        """Check for project data that can exist before the first question."""
        session_id = super()._latest_empty_conversation()
        if session_id is None:
            return None
        files = self.api("/api/krl/selectFilesInfo", {"sessionId": session_id})
        if files not in (None, []):
            return None
        status = _training_status(self.api("/api/krl/queryStatus", {"requestId": session_id}))
        if status in (None, 0):
            return session_id
        return None

    def prepare_question(self, question, identifiers):
        files = program_files(question)
        deadline = time.monotonic() + self.answer_timeout

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise collector.CollectionError(
                    "Project preparation exceeded --answer-timeout; nothing saved"
                )
            return seconds

        def api(path, payload=None, **kwargs):
            seconds = remaining()
            result = self.api(
                path, payload, timeout=(min(20.0, seconds), min(self.request_timeout, seconds)),
                **kwargs,
            )
            remaining()
            return result

        uploaded = api("/api/uploadFiles", files=[
            ("files", (name, source, "application/octet-stream")) for name, source in files
        ])
        if not isinstance(uploaded, list) or len(uploaded) != len(files) or not all(uploaded):
            raise collector.CollectionError("Project upload did not return all program files; nothing saved")
        request = {"requestId": identifiers["requestId"]}
        previous = _training_status(api("/api/krl/queryStatus", request))
        api("/api/krl/train", {
            "filesUrl": uploaded,
            "sessionId": identifiers["sessionId"],
            **request,
            "skillId": str(uuid.uuid4()).replace("-", "_"),
            "trainType": int(bool(previous)) + 1,
        })
        while True:
            status = _training_status(api("/api/krl/queryStatus", request))
            if status == 3:
                raise collector.CollectionError("Project training failed; nothing saved")
            if status == 2:
                break
            time.sleep(min(2.0, remaining()))
        expected = {name.lower() for name, _ in files}
        while True:
            trained = api("/api/krl/selectFilesInfo", {"sessionId": identifiers["sessionId"]})
            if not isinstance(trained, list) or any(not isinstance(item, dict) for item in trained):
                raise collector.CollectionError("Project training returned invalid file details; nothing saved")
            found = {}
            for item in trained:
                name = item.get("name")
                if not isinstance(name, str) or name.lower() in found:
                    raise collector.CollectionError("Project training returned invalid file names; nothing saved")
                found[name.lower()] = item.get("status")
            if set(found) - expected:
                raise collector.CollectionError("Project training returned unexpected program files; nothing saved")
            if any(status is not None and (type(status) is not int or status != 0)
                   for status in found.values()):
                raise collector.CollectionError("A project file failed training; nothing saved")
            if set(found) == expected and all(status == 0 for status in found.values()):
                return
            time.sleep(min(2.0, remaining()))


def build_parser():
    return collector.build_parser(profile=PROFILE)


def main(argv=None):
    return collector.main(argv, profile=PROFILE, client_factory=KrluClient)


if __name__ == "__main__":
    sys.exit(main())
