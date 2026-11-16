"""Welding parameter bindings and profile-specific native stud processes."""

import re
from importlib import import_module

import pytest

validate_syntax = pytest.importorskip(
    "treesitter4robotcode.dialects", reason="requires optional robot-control dependencies",
).validate_syntax
diagnostics = pytest.importorskip(
    "robot_language_lean_checker.welding", reason="requires optional robot-control dependencies",
).diagnostics
generate_process_parameters = import_module("ribosome.application.process_parameters").generate_process_parameters


def parameter(name, index, value, bank="R"):
    return {"name": name, "bank": bank, "index": index, "value": value}


def motion():
    return {"kind": "move", "motion": "MOVL", "target": {"index": 2},
            "speed": 100, "acceleration": 60, "blend": None}


def stud(**updates):
    return {"kind": "stud_weld", "motion": "STUDJ", "target": {"index": 1},
            "speed": 20, "acceleration": 100, "blend": 0,
            "gun": 1, "program": 3, **updates}


def process(dialect="rc5", **updates):
    return {"dialect": dialect, "steps": [motion()], **updates}


def native(**updates):
    return process(**{"application_profile": "SX322013-A2", "steps": [stud()], **updates})


def canonical_lines(code):
    return [re.sub(r"\s*=\s*", "=", line) for line in code.splitlines()]


@pytest.mark.parametrize("dialect", ["rc4", "rc5"])
def test_common_welding_values_bind_explicit_registers_in_both_dialects(dialect):
    bindings = {
        "current_amperes": parameter("current", 10, 180),
        "voltage_volts": parameter("voltage", 11, 24.5),
        "wire_feed_mm_per_second": parameter("wire_feed", 12, 80.125),
        "duration_seconds": parameter("duration", 13, 0.025),
    }
    result = generate_process_parameters(process(
        dialect,
        parameters=[parameter("travel_speed", 1, 100)],
        welding=bindings,
    ))

    assert result.validation["valid"]
    assert validate_syntax(result.code, dialect)["valid"]
    assert {binding.name for binding in result.parameters} == {
        "travel_speed", "current", "voltage", "wire_feed", "duration",
    }
    for binding in bindings.values():
        register = f"R{binding['index']}" if dialect == "rc4" else f"R[{binding['index']}]"
        assert f"SET {register} {binding['value']}" in result.code.splitlines()
    assert "A20_STUD" not in result.code
    assert not any(line.startswith("OUT ") for line in result.code.splitlines())


@pytest.mark.parametrize("field", ["current_amperes", "voltage_volts", "wire_feed_mm_per_second", "duration_seconds"])
def test_each_welding_quantity_can_be_supplied_independently(field):
    result = generate_process_parameters(process(welding={field: parameter("weld_value", 1, 2.5)}))
    assert "SET R[1] 2.5" in result.code
    assert result.validation["valid"]


@pytest.mark.parametrize("field", ["current_amperes", "voltage_volts", "wire_feed_mm_per_second", "duration_seconds"])
@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan")])
def test_welding_quantities_require_explicit_positive_finite_values(field, value):
    with pytest.raises(ValueError):
        generate_process_parameters(process(welding={field: parameter("invalid", 1, value)}))


@pytest.mark.parametrize("welding", [{}, {"current_amperes": None},
                                      {"current_ampere": parameter("current", 1, 100)}])
def test_empty_and_misspelled_welding_configuration_is_rejected(welding):
    with pytest.raises(ValueError):
        generate_process_parameters(process(welding=welding))


@pytest.mark.parametrize("other", [parameter("current", 11, 25), parameter("voltage", 10, 25)])
def test_welding_bindings_cannot_duplicate_names_or_addresses(other):
    with pytest.raises(ValueError):
        generate_process_parameters(process(welding={
            "current_amperes": parameter("current", 10, 100),
            "voltage_volts": other,
        }))


@pytest.mark.parametrize("other", [parameter("current", 1, 25), parameter("travel_speed", 10, 25)])
def test_common_and_welding_bindings_share_one_unique_namespace(other):
    with pytest.raises(ValueError):
        generate_process_parameters(process(
            parameters=[other],
            welding={"current_amperes": parameter("current", 10, 100)},
        ))


def test_welding_bindings_can_supply_motion_parameter_references():
    result = generate_process_parameters(process(
        welding={"wire_feed_mm_per_second": parameter("feed", 1, 75)},
        steps=[{**motion(), "speed": {"parameter": "feed"}}],
    ))
    assert "MOVL P[2] V = R[1] ACC = 60 FINE" in result.code


@pytest.mark.parametrize("dialect", ["rc4", "rc5"])
def test_welding_bindings_obey_controller_register_bounds(dialect):
    with pytest.raises(ValueError):
        generate_process_parameters(process(
            dialect,
            welding={"current_amperes": parameter("current", 10, 100)},
            register_bounds={"R": {"min": 0, "max": 9}},
        ))


def test_stud_process_derives_per_gun_counts_and_keeps_interleaved_motion_order():
    result = generate_process_parameters(native(
        parameters=[parameter("speed", 1, 20.5), parameter("acc", 2, 100), parameter("blend", 3, 0.5)],
        steps=[
            stud(speed={"parameter": "speed"}, acceleration={"parameter": "acc"},
                 blend={"parameter": "blend"}),
            motion(),
            stud(motion="STUDL", target={"index": 3}, speed=10, gun=2, program=4,
                 et=2, interlock=8),
            stud(target={"index": 4}, speed=25, acceleration=80, blend=1, program=10),
        ],
    ))
    lines = canonical_lines(result.code)
    assert "A20_STUD Init Gun=1 Count=2" in lines
    assert "A20_STUD Init Gun=2 Count=1" in lines
    assert lines.count("A20_STUD ProcessCheck") == 1
    assert lines[-2:] == ["A20_STUD ProcessCheck", "END"]
    user_instructions = [line for line in lines if line.startswith(("STUD", "MOVL"))]
    assert user_instructions == [
        "STUDJ P1 VJ=20.5 ACC=100 CNT=0.5 GUN=1 PROGNo=3",
        "MOVL P[2] V=100 ACC=60 FINE",
        "STUDL P3 VL=10 ACC=100 CNT=0 ET=2 INTERLOCK=8 GUN=2 PROGNo=4",
        "STUDJ P4 VJ=25 ACC=80 CNT=1 GUN=1 PROGNo=10",
    ]
    for gun in (1, 2):
        initialization = next(i for i, line in enumerate(lines)
                              if line.startswith(f"A20_STUD Init Gun={gun} "))
        assert all(initialization < i for i, line in enumerate(lines)
                   if line.startswith("STUD") and f"GUN={gun} " in line)
    assert result.application_profile == "SX322013-A2"
    assert validate_syntax(result.code, "rc5")["valid"]
    assert result.validation["valid"]
    assert not diagnostics(result.code, "rc5", "SX322013-A2")
    assert not result.domain_diagnostics
    assert {(resource.bank, resource.index) for resource in result.resources} >= {
        ("P", 1), ("P", 2), ("P", 3), ("P", 4), ("R", 1), ("R", 2), ("R", 3),
    }


@pytest.mark.parametrize("profile", [None, "SX322013-A1", "other"])
def test_native_stud_requires_the_explicit_supported_application_profile(profile):
    with pytest.raises(ValueError):
        generate_process_parameters(native(application_profile=profile))


def test_native_stud_rejects_rc4_instead_of_emitting_rc5_syntax():
    with pytest.raises(ValueError):
        generate_process_parameters(native(dialect="rc4"))


@pytest.mark.parametrize("updates", [
    {"gun": 0}, {"gun": 6}, {"program": 0}, {"program": 4097},
    {"speed": 0}, {"speed": 100.1}, {"acceleration": 0}, {"acceleration": 121},
    {"blend": -0.1}, {"blend": 1.1}, {"blend": None},
    {"gun": True}, {"program": 1.5}, {"speed": True},
    {"target": {"bank": "PR", "index": 1}},
    {"motion": "STUDJ", "et": 1},
    {"motion": "STUDL"}, {"motion": "STUDL", "et": 0}, {"motion": "STUDL", "et": 4},
])
def test_stud_command_enforces_documented_profile_limits_and_command_shape(updates):
    with pytest.raises(ValueError):
        generate_process_parameters(native(steps=[stud(**updates)]))


@pytest.mark.parametrize("updates", [
    {"gun": 1, "program": 1, "speed": 1, "acceleration": 1, "blend": 0},
    {"gun": 5, "program": 4096, "speed": 100, "acceleration": 120, "blend": 1},
    {"motion": "STUDL", "speed": 10000, "et": 1, "interlock": 10000},
    {"motion": "STUDL", "et": 3},
])
def test_stud_profile_accepts_boundary_values_without_invented_linear_speed_or_zone_limits(updates):
    result = generate_process_parameters(native(steps=[stud(**updates)]))
    assert validate_syntax(result.code, "rc5")["valid"]
    assert not diagnostics(result.code, "rc5", "SX322013-A2")


@pytest.mark.parametrize("field", ["speed", "acceleration", "blend"])
def test_stud_parameter_references_are_checked_after_resolution(field):
    with pytest.raises(ValueError):
        generate_process_parameters(native(
            parameters=[parameter("out_of_range", 1, 1000)],
            steps=[stud(**{field: {"parameter": "out_of_range"}})],
        ))


def test_stud_target_and_numeric_bindings_obey_explicit_index_bounds():
    with pytest.raises(ValueError):
        generate_process_parameters(native(register_bounds={"P": {"min": 2, "max": 10}}))
