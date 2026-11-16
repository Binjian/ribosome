"""Generated process operands remain observable through each dialect's parser."""

import pytest

pytest.importorskip("treesitter4robotcode.dialects", reason="requires optional robot-control dependencies")
pytest.importorskip("robot_language_lean_checker.welding", reason="requires optional robot-control dependencies")

from ribosome.application.process_parameters import generate_process_parameters
from treesitter4robotcode.dialects import parse_process_parameters


@pytest.mark.parametrize("dialect", ["rc4", "rc5"])
def test_generated_control_manifest_is_parsed_from_actual_source(dialect):
    result = generate_process_parameters({
        "dialect": dialect,
        "parameters": [{"name": "speed", "bank": "R", "index": 3, "value": 125.5}],
        "tool_frame": 1,
        "user_frame": 2,
        "steps": [
            {"kind": "move", "motion": "MOVL", "target": {"index": 2},
             "speed": {"parameter": "speed"}, "acceleration": 60, "blend": None},
            {"kind": "output", "channel": 8, "value": True},
            {"kind": "wait_input", "channel": 9, "value": True, "timeout": 500},
        ],
    })
    parsed = result.parsed_parameters
    assert parsed == parse_process_parameters(result.code, dialect)
    assert result.validation == {"valid": True, "diagnostics": []}
    assert [item["kind"] for item in parsed["instructions"]] == [
        "binding", "frame", "frame", "motion", "output", "wait",
    ]
    binding, *_, wait = parsed["instructions"]
    assert binding["fields"]["source"][0]["text"] == "125.5"
    assert wait["fields"]["timeout"][0]["text"] == "500"
    motion = parsed["instructions"][3]
    target = "P2" if dialect == "rc4" else "P[2]"
    speed = "R3" if dialect == "rc4" else "R[3]"
    assert motion["fields"]["target"][0]["text"] == target
    # The manifest preserves the runtime read, rather than replacing it with 125.5.
    assert speed in motion["text"]
    assert "125.5" not in motion["text"]
    assert motion["start"] == [5, 1]


def test_generated_stud_records_expose_per_gun_counts_and_real_targets():
    result = generate_process_parameters({
        "dialect": "rc5", "application_profile": "SX322013-A2",
        "steps": [
            {"kind": "stud_weld", "motion": "STUDJ", "target": {"index": 10},
             "speed": 25, "acceleration": 60, "blend": 0, "gun": 1, "program": 3},
            {"kind": "stud_weld", "motion": "STUDL", "target": {"index": 11},
             "speed": 100, "acceleration": 60, "blend": 0, "gun": 1, "program": 3,
             "et": 1, "interlock": 2},
        ],
    })
    records = result.parsed_parameters["instructions"]
    assert [item["kind"] for item in records] == ["stud_init", "stud_weld", "stud_weld", "stud_check"]
    assert records[0]["fields"]["count"][0]["text"] == "2"
    assert records[1]["fields"]["target"][0]["text"] == "P10"
    assert records[2]["fields"]["target"][0]["text"] == "P11"
    assert records[2]["fields"]["et"][0]["text"] == "1"
    assert result.domain_diagnostics == []


def test_invalid_candidate_has_no_partial_parameter_manifest():
    result = parse_process_parameters("NOP\nSET R[3] 125.5\nMOVL P[2] V = 10\nEND", "rc5")
    assert result["valid"] is False
    assert result["diagnostics"]
    assert result["instructions"] == []
