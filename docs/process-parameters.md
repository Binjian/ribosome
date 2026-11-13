# RC4 and RC5 process-parameter generation

[`ribosome.application.process_parameters`](../nbs/05.application.process_parameters.ipynb)
turns explicit control and welding settings into a complete `NOP`…`END`
instruction program. It selects the existing RC4 or RC5 parser and validates
the generated syntax before returning source and a resource manifest. The module
uses no model, database, controller connection, or environment configuration.

Supported common operations are numeric-register initialization, tool/user-frame
selection, `MOVJ`/`MOVL`, digital outputs, and timed digital-input waits. Welding
support includes register-bound process settings for both dialects and native
RC5 stud-welding steps for the explicitly selected `SX322013-A2` application
profile.

Positions and frames identify existing taught resources. The generator does not
invent point coordinates, calculate trajectories, optimize a welding recipe, or
choose machine-specific register mappings. Syntax validation and the documented
stud-parameter checks do not establish execution behavior or physical safety.

## Notebook source and preparation

The [implementation notebook](../nbs/05.application.process_parameters.ipynb)
exports `ribosome.application.process_parameters`. Work through the executable
[tutorial](../nbs/06.tutorial.process_parameters.ipynb) to compare both dialects,
inspect resource manifests, configure welding bindings, and generate multi-gun
stud operations. The tutorial contains examples only and is not exported.

Use Python 3.12 or 3.13, Pydantic 2, and the `treesitter4robotcode` and
`robot-language-lean-checker` packages. The backend checkout already declares the
parser and checker as editable dependencies. A base standalone Ribosome
installation does not include these companion packages; install them in the
same environment using their checkout instructions, or use the backend environment.
Native parameter diagnostics use
the checker's Python module; generation does not invoke a Lean executable.

From the backend repository root:

```sh
uv run python tools/prepare_ribosome.py
```

From a standalone Ribosome checkout with the dependencies installed:

```sh
python -m ribosome.prepare --profile standalone
```

Both commands export source without executing notebook examples. The backend
profile includes this process-parameter module alongside its RAG dependencies.
`llm_backend.process_parameters` remains a compatibility re-export of this API.
Edit the notebook when changing the implementation, then prepare the runtime
module again; generated Python is not the source of truth.

## Common control example

Call `generate_process_parameters` with a dictionary or a validated `ProcessSpec`.
The dialect is required; it is never inferred from source or parameter names.

```python
from ribosome.application.process_parameters import generate_process_parameters

result = generate_process_parameters({
    "dialect": "rc5",
    "parameters": [
        {"name": "travel_speed", "bank": "R", "index": 1, "value": 50.0},
    ],
    "tool_frame": 1,
    "user_frame": 1,
    "register_bounds": {
        "R": {"min": 1, "max": 10},
        "P": {"min": 1, "max": 10},
        "TF": {"min": 1, "max": 2},
        "UF": {"min": 1, "max": 2},
        "DO": {"min": 1, "max": 8},
        "DI": {"min": 1, "max": 8},
    },
    "steps": [
        {
            "kind": "move", "motion": "MOVJ",
            "target": {"bank": "P", "index": 1},
            "speed": {"parameter": "travel_speed"},
            "acceleration": 80, "blend": None,
        },
        {"kind": "output", "channel": 1, "value": True},
        {"kind": "wait_input", "channel": 1, "value": True, "timeout": 100},
    ],
})

print(result.code)
assert result.validation["valid"]
assert not result.unbounded_banks
```

Generated source:

```text
NOP
SET R[1] 50.0
SETFRAME TF 1
SETFRAME UF 1
MOVJ P[1] V = R[1] ACC = 80 FINE
OUT DO[1] 1
WAIT DI[1] = 1 T = 100
END
```

For RC4, use `"dialect": "rc4"` and bounds named `OT`/`IN` in place of `DO`/`DI`.
The same operations render as:

```text
NOP
SET R1 50.0
TF #1
UF #1
MOVJ P1 VJ=R1 ACC=80 CNT=0
OUT OT#1=ON
WAIT IN#1=ON T=100
END
```

The values and index ranges above illustrate the interface; supply settings and
ranges appropriate to the actual cell.

## Register-bound welding settings in either dialect

`welding` accepts any nonempty subset of these fields. Each field is a
`NumericParameter` with an explicit name, `I` or `R` bank, index, and positive
value.

| Field | Unit |
| --- | --- |
| `current_amperes` | A |
| `voltage_volts` | V |
| `wire_feed_mm_per_second` | mm/s |
| `duration_seconds` | s |

These fields initialize the selected numeric registers. The mapping from a
register to equipment behavior must already be configured in the cell; writing a
register does not by itself start welding or configure a power supply protocol.
There are no inferred process values or generic current, voltage, wire-feed, or
duration limits.

```python
from ribosome.application.process_parameters import generate_process_parameters

for dialect in ("rc4", "rc5"):
    result = generate_process_parameters({
        "dialect": dialect,
        "welding": {
            "current_amperes": {
                "name": "weld_current", "bank": "R", "index": 10, "value": 120.0,
            },
            "voltage_volts": {
                "name": "weld_voltage", "bank": "R", "index": 11, "value": 20.0,
            },
            "wire_feed_mm_per_second": {
                "name": "wire_feed", "bank": "R", "index": 12, "value": 50.0,
            },
            "duration_seconds": {
                "name": "weld_duration", "bank": "R", "index": 13, "value": 0.5,
            },
        },
        "register_bounds": {"R": {"min": 10, "max": 13}},
        "steps": [{"kind": "output", "channel": 1, "value": False}],
    })
    print(dialect, result.code)
```

For example, current initialization becomes `SET R10 120.0` in RC4 and
`SET R[10] 120.0` in RC5. All four bindings appear in `result.parameters` and
`result.resources`. The output channel has no supplied bounds in this example,
so `result.unbounded_banks` reports `OT` or `DO`. The example numbers and output
channel are placeholders for caller-supplied cell settings, not a welding recipe.

## Native RC5 stud welding

Native stud steps require `"dialect": "rc5"` and
`"application_profile": "SX322013-A2"`. The RC4 grammar has no native stud-command
support, so RC4 native stud requests are rejected; register-bound welding
settings remain available in both dialects.

```python
from ribosome.application.process_parameters import generate_process_parameters

result = generate_process_parameters({
    "dialect": "rc5",
    "application_profile": "SX322013-A2",
    "steps": [
        {
            "kind": "stud_weld", "motion": "STUDJ",
            "target": {"bank": "P", "index": 1},
            "speed": 20, "acceleration": 100, "blend": 0,
            "gun": 1, "program": 3,
        },
        {
            "kind": "stud_weld", "motion": "STUDL",
            "target": {"bank": "P", "index": 2},
            "speed": 200, "acceleration": 100, "blend": 0,
            "gun": 1, "program": 3, "et": 1, "interlock": 2,
        },
    ],
})

assert result.validation["valid"]
assert not result.domain_diagnostics
print(result.code)
```

The generator derives each gun's `A20_STUD Init` count from the supplied
straight-line stud steps and adds `A20_STUD ProcessCheck`. Two weld steps for
gun 1 therefore initialize its count to 2. Native stud targets use the
application syntax `P1`/`P2`, including in RC5. `PR` targets are rejected.

The existing application checker enforces the documented ranges for this profile:

| Parameter | Range |
| --- | --- |
| `gun` | 1–5 |
| `program` (`PROGNo`) | 1–4096 |
| `STUDJ` speed (`VJ`, percent) | 1–100 |
| Acceleration (`ACC`) | 1–120 |
| Blend (`CNT`) | 0–1 |
| `et` (`ET`) | 1–3; required for `STUDL`, forbidden for `STUDJ` |

`STUDL` speed is `VL` in mm/s; no undocumented maximum is imposed.
`interlock` selects an interference-zone file and must be a nonnegative integer;
it is not a Boolean enable flag. The module does not derive welding-program
settings from current, voltage, or wire-feed bindings. The Python diagnostics come from `robot_language_lean_checker.welding`,
which cites SX322013 A-2 §§2.1–2.2 and table 3-2. Execution checking remains a
separate integration step.

## Validation and returned data

The models reject unknown fields, duplicate names, duplicate numeric-register
bindings, unknown parameter references, booleans in numeric fields, and
nonfinite numbers. Integer-register values must be integers. All resource
indices must be nonnegative integers; configured `register_bounds` apply to
every used resource, including positions, frames, and I/O. No default controller
index limits are assumed. Bounds must use the selected dialect's bank names.

Motion speed and acceleration must be positive; blend values must be
nonnegative. A common motion's required `blend=None` requests an exact stop,
rendered as RC5 `FINE` or RC4 `CNT=0`; numeric blend values render as `CNT`.
Native stud steps require numeric blend values. Numeric literals use decimal
notation accepted by the parsers, including values Python normally prints with
an exponent.

Common motion speed references remain runtime register reads. References used
for acceleration, blend, and WAIT timeout are resolved to literal values when
generating. Native stud operands follow their application grammar and are also
rendered as literals. Each named binding is initialized before the steps.

Input-wait timeouts must be finite and positive and use the selected controller's
native `WAIT T` units. The generator performs no conversion from seconds or
milliseconds. Infinite waits such as `T=-1` are outside this interface.

`GeneratedProcess` contains:

| Field | Content |
| --- | --- |
| `dialect` | Explicit normalized RC4/RC5 dialect |
| `code` | Complete generated instruction source, without a job wrapper |
| `parameters` | Numeric bindings, values, names, and rendered register references |
| `resources` | Deduplicated registers, positions, frames, and I/O used by the program |
| `unbounded_banks` | Used banks with no caller-supplied index bounds |
| `validation` | Syntax-check result for the selected parser |
| `application_profile` | Selected native application profile, if any |
| `domain_diagnostics` | Documented application diagnostics |

Invalid specifications or generated source raise `ValueError` (including
Pydantic `ValidationError`) instead of returning an invalid program. The output
can be inspected or passed to existing validation workflows; the generator
does not execute, upload, or publish it.
