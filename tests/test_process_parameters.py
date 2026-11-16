"""Deterministic process generation without storage, models, or hardware."""

from copy import deepcopy
from importlib import import_module

import pytest

validate_syntax = pytest.importorskip(
    "treesitter4robotcode.dialects", reason="requires optional robot-control dependencies",
).validate_syntax
pytest.importorskip("robot_language_lean_checker.welding", reason="requires optional robot-control dependencies")
_parameters = import_module("ribosome.application.process_parameters")
NumericParameter = _parameters.NumericParameter
ProcessSpec = _parameters.ProcessSpec
generate_process_parameters = _parameters.generate_process_parameters


def move(**updates):
    return {
        "kind": "move",
        "motion": "MOVL",
        "target": {"index": 2},
        "speed": 125.5,
        "acceleration": 60,
        "blend": None,
        **updates,
    }


def process(dialect="rc5", **updates):
    return {"dialect": dialect, "steps": [move()], **updates}


@pytest.mark.parametrize(
    "dialect, expected",
    [
        (
            "rc4",
            [
                "NOP",
                "SET R3 125.5",
                "SET I4 60",
                "TF #1",
                "UF #2",
                "OUT OT#8=OFF",
                "MOVJ PR7 VJ=25 ACC=60 CNT=0",
                "MOVL P2 VL=R3 ACC=60 CNT=0.25",
                "OUT OT#8=ON",
                "WAIT IN#9=ON T=500",
                "END",
            ],
        ),
        (
            "rc5",
            [
                "NOP",
                "SET R[3] 125.5",
                "SET I[4] 60",
                "SETFRAME TF 1",
                "SETFRAME UF 2",
                "OUT DO[8] 0",
                "MOVJ PR[7] V = 25 ACC = 60 FINE",
                "MOVL P[2] V = R[3] ACC = 60 CNT = 0.25",
                "OUT DO[8] 1",
                "WAIT DI[9] = 1 T = 500",
                "END",
            ],
        ),
    ],
)
def test_complete_process_preserves_operation_order_and_uses_matching_grammar(dialect, expected):
    specification = process(
        dialect,
        parameters=[
            {"name": "travel_speed", "bank": "R", "index": 3, "value": 125.5},
            {"name": "acceleration", "bank": "I", "index": 4, "value": 60},
        ],
        tool_frame=1,
        user_frame=2,
        steps=[
            {"kind": "output", "channel": 8, "value": False},
            move(motion="MOVJ", target={"bank": "PR", "index": 7}, speed=25,
                 acceleration={"parameter": "acceleration"}),
            move(speed={"parameter": "travel_speed"},
                 acceleration={"parameter": "acceleration"}, blend=0.25),
            {"kind": "output", "channel": 8, "value": True},
            {"kind": "wait_input", "channel": 9, "value": True, "timeout": 500},
        ],
    )
    original = deepcopy(specification)
    result = generate_process_parameters(specification)

    assert result.dialect == dialect
    assert result.code.splitlines() == expected
    assert result.validation["valid"]
    assert validate_syntax(result.code, dialect)["valid"]
    assert specification == original
    assert {(resource.bank, resource.index) for resource in result.resources} == {
        ("R", 3), ("I", 4), ("TF", 1), ("UF", 2), ("PR", 7), ("P", 2),
        ("OT" if dialect == "rc4" else "DO", 8),
        ("IN" if dialect == "rc4" else "DI", 9),
    }


@pytest.mark.parametrize("dialect", ["rc4", "rc5"])
def test_parameter_references_in_literal_only_slots_are_resolved(dialect):
    result = generate_process_parameters(process(
        dialect,
        parameters=[
            {"name": "acc", "bank": "R", "index": 1, "value": 32.5},
            {"name": "rounding", "bank": "R", "index": 2, "value": 0.125},
            {"name": "timeout", "bank": "I", "index": 1, "value": 250},
        ],
        steps=[move(acceleration={"parameter": "acc"}, blend={"parameter": "rounding"}),
               {"kind": "wait_input", "channel": 1, "value": False,
                "timeout": {"parameter": "timeout"}}],
    ))
    assert validate_syntax(result.code, dialect)["valid"]
    if dialect == "rc5":
        assert "ACC = 32.5 CNT = 0.125" in result.code
        assert "WAIT DI[1] = 0 T = 250" in result.code
    else:
        assert "ACC=32.5 CNT=0.125" in result.code
        assert "WAIT IN#1=OFF T=250" in result.code


@pytest.mark.parametrize("dialect", ["rc4", "rc5"])
@pytest.mark.parametrize("value, rendered", [(1e-7, "0.0000001"), (1e20, "100000000000000000000"),
                                               (1.2345678901234567, "1.2345678901234567")])
def test_numeric_rendering_keeps_precision_without_scientific_notation(dialect, value, rendered):
    result = generate_process_parameters(process(dialect, steps=[move(speed=value)]))
    motion_line = next(line for line in result.code.splitlines() if line.startswith("MOVL"))
    assert rendered in motion_line
    assert "e-" not in motion_line.lower() and "e+" not in motion_line.lower()
    assert validate_syntax(result.code, dialect)["valid"]


def test_explicit_dialect_is_normalized_and_never_inferred():
    assert generate_process_parameters(process(" RC4 ")).dialect == "rc4"
    for dialect in ("rc6", "NA", None, 5):
        with pytest.raises(ValueError):
            generate_process_parameters(process(dialect))
    with pytest.raises(ValueError):
        generate_process_parameters({"steps": [move()]})


@pytest.mark.parametrize("field", ["target", "speed", "acceleration", "blend", "motion"])
def test_motion_requires_explicit_parameters(field):
    step = move()
    del step[field]
    with pytest.raises(ValueError):
        generate_process_parameters(process(steps=[step]))


@pytest.mark.parametrize("steps", [[], [{"kind": "weld"}], [move(target={})],
                                    [move(target="P2")], [move(motion="MOVC")],
                                    [move(speed="R1")], [move(speed="1\nEND")]])
def test_unsupported_or_underspecified_operations_are_rejected(steps):
    with pytest.raises(ValueError):
        generate_process_parameters(process(steps=steps))


@pytest.mark.parametrize("field", ["speed", "acceleration", "blend"])
@pytest.mark.parametrize("value", [True, False, float("inf"), float("-inf"), float("nan"), "10"])
def test_motion_numbers_are_finite_and_never_coerced(field, value):
    with pytest.raises(ValueError):
        generate_process_parameters(process(steps=[move(**{field: value})]))


@pytest.mark.parametrize("field, value", [("speed", 0), ("speed", -1),
                                           ("acceleration", 0), ("acceleration", -1),
                                           ("blend", -0.1)])
def test_motion_parameters_reject_nonphysical_signs(field, value):
    with pytest.raises(ValueError):
        generate_process_parameters(process(steps=[move(**{field: value})]))


@pytest.mark.parametrize("field", ["speed", "acceleration", "blend"])
def test_resolved_parameters_are_validated_at_their_use(field):
    with pytest.raises(ValueError):
        generate_process_parameters(process(
            parameters=[{"name": "negative", "bank": "R", "index": 1, "value": -2.5}],
            steps=[move(**{field: {"parameter": "negative"}})],
        ))


@pytest.mark.parametrize("field", ["speed", "acceleration", "blend"])
def test_undefined_parameter_reference_is_rejected(field):
    with pytest.raises(ValueError):
        generate_process_parameters(process(steps=[move(**{field: {"parameter": "missing"}})]))


@pytest.mark.parametrize("duplicate", [
    {"name": "speed", "bank": "R", "index": 2, "value": 50},
    {"name": "other_speed", "bank": "R", "index": 1, "value": 50},
])
def test_parameter_names_and_register_addresses_are_unique(duplicate):
    with pytest.raises(ValueError):
        generate_process_parameters(process(parameters=[
            {"name": "speed", "bank": "R", "index": 1, "value": 25}, duplicate,
        ]))


@pytest.mark.parametrize("parameter", [
    {"name": "fraction", "bank": "I", "index": 1, "value": 2.5},
    {"name": "bad", "bank": "R", "index": -1, "value": 2},
    {"name": "bad", "bank": "R", "index": True, "value": 2},
    {"name": "bad", "bank": "R", "index": 1, "value": True},
    {"name": "bad", "bank": "R", "index": 1, "value": float("inf")},
    {"name": "bad", "bank": "R", "index": 1, "value": float("nan")},
    {"name": "bad", "bank": "P", "index": 1, "value": 2},
    {"name": "speed\nEND", "bank": "R", "index": 1, "value": 2},
])
def test_invalid_numeric_parameter_binding_is_rejected(parameter):
    with pytest.raises(ValueError):
        generate_process_parameters(process(parameters=[parameter]))


@pytest.mark.parametrize("step", [
    {"kind": "output", "channel": 1, "value": 1},
    {"kind": "output", "channel": -1, "value": True},
    {"kind": "output", "channel": True, "value": True},
    {"kind": "wait_input", "channel": 1, "value": True},
    {"kind": "wait_input", "channel": 1, "value": True, "timeout": 0},
    {"kind": "wait_input", "channel": 1, "value": True, "timeout": -1},
    {"kind": "wait_input", "channel": 1, "value": True, "timeout": True},
    {"kind": "wait_input", "channel": 1, "value": True, "timeout": {"parameter": "missing"}},
])
def test_io_and_wait_require_explicit_valid_parameters(step):
    with pytest.raises(ValueError):
        generate_process_parameters(process(steps=[step]))


@pytest.mark.parametrize("dialect, input_bank, output_bank", [("rc4", "IN", "OT"), ("rc5", "DI", "DO")])
def test_explicit_profile_covers_every_generated_resource(dialect, input_bank, output_bank):
    bounds = {bank: {"min": 1, "max": 10}
              for bank in ("R", "P", "TF", "UF", input_bank, output_bank)}
    result = generate_process_parameters(process(
        dialect,
        parameters=[{"name": "speed", "bank": "R", "index": 1, "value": 2}],
        tool_frame=1, user_frame=1,
        steps=[move(speed={"parameter": "speed"}),
               {"kind": "output", "channel": 1, "value": True},
               {"kind": "wait_input", "channel": 1, "value": True, "timeout": 100}],
        register_bounds=bounds,
    ))
    assert not result.unbounded_banks
    assert result.validation["valid"]


@pytest.mark.parametrize("specification", [
    process(register_bounds={"P": {"min": 3, "max": 4}}),
    process(tool_frame=5, register_bounds={"TF": {"min": 0, "max": 4}}),
    process(user_frame=5, register_bounds={"UF": {"min": 0, "max": 4}}),
    process(parameters=[{"name": "speed", "bank": "R", "index": 5, "value": 1}],
            register_bounds={"R": {"min": 0, "max": 4}}),
    process(steps=[{"kind": "output", "channel": 5, "value": True}],
            register_bounds={"DO": {"min": 0, "max": 4}}),
    process(steps=[{"kind": "wait_input", "channel": 5, "value": True, "timeout": 100}],
            register_bounds={"DI": {"min": 0, "max": 4}}),
])
def test_out_of_profile_indices_are_rejected(specification):
    with pytest.raises(ValueError):
        generate_process_parameters(specification)


@pytest.mark.parametrize("dialect, wrong_bank", [("rc4", "DO"), ("rc4", "DI"),
                                                 ("rc5", "OT"), ("rc5", "IN")])
def test_profile_bank_names_cannot_leak_across_dialects(dialect, wrong_bank):
    with pytest.raises(ValueError):
        generate_process_parameters(process(dialect, register_bounds={wrong_bank: {"min": 0, "max": 10}}))


@pytest.mark.parametrize("bounds", [
    {"P": {"min": 5, "max": 1}}, {"P": {"min": -1, "max": 1}},
    {"P": {"min": False, "max": 1}}, {"P": {"min": 0, "max": 1.5}},
    {"P": {"min": 0}}, {"unknown": {"min": 0, "max": 10}},
])
def test_malformed_profile_is_rejected(bounds):
    with pytest.raises(ValueError):
        generate_process_parameters(process(register_bounds=bounds))


def test_missing_controller_bounds_remain_explicit():
    result = generate_process_parameters(process())
    assert "P" in result.unbounded_banks
    assert result.code.splitlines() == ["NOP", "MOVL P[2] V = 125.5 ACC = 60 FINE", "END"]


def test_model_inputs_are_supported_and_mutated_nested_containers_are_revalidated():
    specification = ProcessSpec.model_validate(process(
        parameters=[{"name": "speed", "bank": "R", "index": 1, "value": 25}],
    ))
    assert generate_process_parameters(specification).validation["valid"]
    # Frozen Pydantic attributes do not freeze the contents of nested lists.
    specification.parameters.append(NumericParameter(name="duplicate", bank="R", index=1, value=50))
    with pytest.raises(ValueError):
        generate_process_parameters(specification)


@pytest.mark.parametrize("updates", [{"typo": 1}, {"tool_frame": True}, {"user_frame": -1},
                                        {"steps": [move(extra="unexpected")]}])
def test_unknown_fields_and_bad_frame_indices_are_not_silently_accepted(updates):
    with pytest.raises(ValueError):
        generate_process_parameters(process(**updates))
