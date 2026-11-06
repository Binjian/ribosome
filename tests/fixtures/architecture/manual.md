# Synthetic service manual — revision A

ALM-204 means the fixture valve is unavailable. The limit is 12 V, not 24 V.

## Reset procedure

<table>
<thead><tr><th>Task</th><th>Identifier</th><th>Action</th></tr></thead>
<tbody>
<tr><td rowspan="2">Reset</td><td>RST-001</td><td>Check that power is off.</td></tr>
<tr><td>RST-002</td><td>Restore the valve only after inspection.</td></tr>
</tbody>
</table>

1. Inspect the valve.
   - Locate the connector in ![Valve connector](diagram.svg).
   - Preserve the serial number `FIXTURE-42`.
2. Run the synthetic command below.

```text
CHECK POWER OFF
RESET VALVE RST-002
VERIFY STATUS READY
```

The fixture formula is $V = I R$.

> | Parameter | Value |
> | --- | --- |
> | Voltage | 12 V |

Literal citation-like source text: [E999]. This is not an issued citation.
