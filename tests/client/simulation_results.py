"""Validate every reported scenario before named simulation tests select their rows."""
import json


def require_successful_scenarios(rows, process):
    assert isinstance(rows, list) and rows, "simulation must report a nonempty list of scenarios"
    names = set()
    for row in rows:
        assert isinstance(row, dict), f"invalid simulation scenario: {row!r}"
        name = row.get("name")
        assert isinstance(name, str) and name, f"scenario is missing its name: {row!r}"
        assert name not in names, f"duplicate simulation scenario: {name}"
        names.add(name)
        assert row.get("ok") is True, f"{name}: {json.dumps(row.get('detail', row))}"
    assert process.returncode == 0, f"simulation exited {process.returncode}: {process.stderr[-2000:]}"
    return {row["name"]: row for row in rows}
