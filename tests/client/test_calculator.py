"""Calculator — the SHIPPED static/js/client/calculator.js.

The evaluator is a parser, never eval(): what is typed is arithmetic, and eval on it would run
whatever else is typed or pasted with this origin's session in reach. These run the file under node
and pin the arithmetic a person checks first (precedence, implicit multiplication, percent, degrees)
and the refusals.
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "static/js/client/calculator.js"


def _run(cases, deg=True):
    js = r"""
global.localStorage={getItem(){return null},setItem(){},removeItem(){}};
const C=require(process.argv[1]); const cases=JSON.parse(process.argv[2]); const deg=process.argv[3]==='1';
process.stdout.write(JSON.stringify(cases.map(e=>{ try{ return {v:C.format(C.evaluate(e,{deg}))}; }catch(x){ return {err:x.message}; } })));
"""
    r = subprocess.run(["node", "-e", js, str(CALC), json.dumps(cases), "1" if deg else "0"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("expr,want", [
    ("12+3*4", "24"), ("(12+3)*4", "60"), ("2^3^2", "512"), ("-2^2", "-4"), ("0.1+0.2", "0.3"),
    ("10/4", "2.5"), ("7%3", "1"), ("50%", "0.5"), ("200*15%", "30"), ("2π", "6.28318530718"),
    ("3(4+1)", "15"), ("(1+1)(2+2)", "8"), ("5!", "120"), ("3²", "9"), ("sqrt(16)", "4"), ("√(2)²", "2"),
    ("sin(30)", "0.5"), ("cos(60)", "0.5"), ("log(1000)", "3"), ("ln(e)", "1"), ("12×3÷4−1", "8"),
    ("1234567*1000", "1,234,567,000"), ("1e20*10", "1e21"), ("abs(-3)", "3"),
])
def test_arithmetic(expr, want):
    assert _run([expr])[0] == {"v": want}, expr


def test_radians_mode():
    assert _run(["sin(pi/2)", "cos(0)"], deg=False) == [{"v": "1"}, {"v": "1"}]


@pytest.mark.parametrize("expr", ["1/0", "2+", "(1+2", "1+2)", "alert(1)", "constructor", "this", "1;2",
                                  "tan(90)", "3.5!", "__proto__", "fetch('x')"])
def test_refusals_are_errors_not_code(expr):
    out = _run([expr])[0]
    assert "err" in out, f"{expr!r} should be refused, got {out}"
