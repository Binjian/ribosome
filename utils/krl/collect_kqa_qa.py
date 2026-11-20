#!/usr/bin/env python3
"""Collect KUKA Center 智能问答 answers into resumable Markdown batches.

Run ``python utils/krl/collect_kqa_qa.py --dry-run`` first. This entry point
shares login, empty-conversation recovery, streaming, validation, retries,
concurrency, and atomic storage with the KRL collector. Credentials come from
utils/krl/.env. Use --auto-captcha to read and submit the site's image CAPTCHA
locally, or enter it at the prompt.
"""

from __future__ import annotations

import sys

if __package__:
    from . import collect_krl_qa as collector
else:
    import collect_krl_qa as collector


PROFILE = collector.CollectionProfile(
    key="kqa",
    mode="智能问答",
    robot_model="intelligent_customer_service_robot",
    questions=collector.ROOT / "assets/KQA/kqa_questions_1000.md",
    output=collector.ROOT / "assets/KQA/kqa_qa_collected.md",
    start=1,
    title="KQA智能问答采集",
    # Both frontend tabs use this endpoint, including 智能问答.
    answer_path="/api/krl/selectQuestionInfo",
)


def build_parser():
    return collector.build_parser(profile=PROFILE)


def main(argv=None, *, retry_failed_runs=False):
    return collector.main(argv, profile=PROFILE, retry_failed_runs=retry_failed_runs)


if __name__ == "__main__":
    sys.exit(main(retry_failed_runs=True))
