import itertools
import re
import subprocess
import tempfile
from pathlib import Path

import sympy as sp
from sympy.logic.boolalg import And, Or, Not, Xor, simplify_logic
from flask import Flask, request, render_template_string, Response

app = Flask(__name__)
last_result = {}

PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Boolean Circuit Optimizer</title>
<style>
body{font-family:Arial;background:#f1f5fb;color:#172033;
max-width:900px;margin:30px auto;padding:16px}
.card{background:white;padding:24px;margin:16px 0;border-radius:12px}
textarea{width:100%;height:70px;font-size:16px;padding:10px;box-sizing:border-box}
button{background:#1759b5;color:white;border:0;padding:12px 18px;
border-radius:7px;cursor:pointer;margin-top:10px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f8;padding:12px}
.good{color:green}.bad{color:#b42318}
</style>
</head>
<body>
<div class="card">
<h1>Boolean Circuit Optimizer &amp; Simulator</h1>
<p>Simplify Boolean expressions, generate Verilog and verify simulated outputs.</p>
<form method="post">
<label>Enter expression (use A–F, &, |, ~ and parentheses)</label>
<textarea name="expression" required>{{ expression }}</textarea>
<button>Optimize &amp; Simulate</button>
</form>
{% if error %}<p class="bad">{{ error }}</p>{% endif %}
</div>
{% if result %}
<div class="card">
<h2>Optimization Results</h2>
<p><b>Original:</b> {{ result.original }}</p>
<p><b>Optimized:</b> {{ result.optimized }}</p>
<p><b>Boolean equivalence:</b> {{ result.equivalent }}</p>
<h3>Generated Verilog</h3><pre>{{ result.verilog }}</pre>
<h3>Generated Testbench</h3><pre>{{ result.testbench }}</pre>
<h3>Simulation Verification</h3>
<p class="{{ 'good' if result.passed else 'bad' }}">
<b>{{ 'PASSED' if result.passed else 'FAILED' }}</b></p>
<pre>{{ result.report }}</pre>
<a href="/download/verilog">Download Verilog</a> |
<a href="/download/testbench">Download Testbench</a> |
<a href="/download/report">Download Report</a>
</div>
{% endif %}
</body>
</html>
"""


def convert_verilog(expr):
    if expr == sp.true:
        return "1'b1"
    if expr == sp.false:
        return "1'b0"
    if getattr(expr, "is_Symbol", False):
        return str(expr)
    if isinstance(expr, Not):
        return f"~({convert_verilog(expr.args[0])})"
    if isinstance(expr, And):
        return "(" + " & ".join(convert_verilog(x) for x in expr.args) + ")"
    if isinstance(expr, Or):
        return "(" + " | ".join(convert_verilog(x) for x in expr.args) + ")"
    if isinstance(expr, Xor):
        return "(" + " ^ ".join(convert_verilog(x) for x in expr.args) + ")"
    raise ValueError("Unsupported Boolean expression.")


def make_verilog(expr, variables):
    names = [str(v) for v in variables]
    return (
        f"module optimized_circuit({', '.join(names + ['F'])});\n"
        + "\n".join(f"input wire {n};" for n in names)
        + "\noutput wire F;\n"
        + f"assign F = {convert_verilog(expr)};\nendmodule\n"
    )


def make_testbench(variables):
    names = [str(v) for v in variables]
    declarations = "\n".join(f"reg {n};" for n in names)
    connections = ", ".join(
        [f".{n}({n})" for n in names] + [".F(F)"]
    )
    lines = []
    for values in itertools.product([0, 1], repeat=len(names)):
        lines.extend(
            f"    {n} = 1'b{v};" for n, v in zip(names, values)
        )
        lines.append(
            '    #1; $display("INPUTS=%b OUTPUT=%b", {' +
            ", ".join(names) + '}, F);'
        )
    return (
        "`timescale 1ns/1ps\nmodule tb;\n"
        + declarations + "\nwire F;\n"
        + f"optimized_circuit dut ({connections});\n"
        + "initial begin\n" + "\n".join(lines)
        + "\n    $finish;\nend\nendmodule\n"
    )


def run_project(expression):
    if len(expression) > 200:
        raise ValueError("Expression is too long.")
    if not re.fullmatch(r"[A-Fa-f01\s&|~()]+", expression):
        raise ValueError("Use only variables A–F, 0, 1, &, |, ~ and parentheses.")

    expression = expression.upper()
    names = sorted(set(re.findall(r"\b[A-F]\b", expression)))
    if not names:
        raise ValueError("Enter an expression using variables A–F.")

    symbols = {n: sp.Symbol(n) for n in names}
    original = sp.sympify(expression, locals=symbols)
    variables = [symbols[n] for n in names]
    optimized = simplify_logic(original, form="dnf", force=True)

    equivalent = True
    for values in itertools.product([0, 1], repeat=len(names)):
        assignment = {v: bool(x) for v, x in zip(variables, values)}
        if bool(original.subs(assignment)) != bool(optimized.subs(assignment)):
            equivalent = False

    verilog = make_verilog(optimized, variables)
    testbench = make_testbench(variables)

    with tempfile.TemporaryDirectory() as temp:
        design = Path(temp) / "design.v"
        tb = Path(temp) / "testbench.v"
        executable = Path(temp) / "simulation.out"
        design.write_text(verilog)
        tb.write_text(testbench)

        compiled = subprocess.run(
            ["iverilog", "-o", str(executable), str(design), str(tb)],
            capture_output=True, text=True, timeout=20
        )
        if compiled.returncode:
            raise RuntimeError("Compilation failed: " + compiled.stderr)

        simulated = subprocess.run(
            ["vvp", str(executable)],
            capture_output=True, text=True, timeout=20
        )
        if simulated.returncode:
            raise RuntimeError("Simulation failed: " + simulated.stderr)

    matches = re.findall(r"INPUTS=([01]+)\s+OUTPUT=([01])", simulated.stdout)
    if len(matches) != 2 ** len(names):
        raise RuntimeError("Unexpected simulation output.")

    passed = equivalent
    lines = ["INPUTS | EXPECTED | SIMULATED | STATUS"]
    for bits, actual in matches:
        assignment = {v: bool(int(b)) for v, b in zip(variables, bits)}
        expected = int(bool(original.subs(assignment)))
        row_passed = expected == int(actual)
        passed = passed and row_passed
        lines.append(
            f"{bits} | {expected} | {actual} | "
            f"{'PASS' if row_passed else 'FAIL'}"
        )

    return {
        "original": str(original),
        "optimized": str(optimized),
        "equivalent": "YES" if equivalent else "NO",
        "verilog": verilog,
        "testbench": testbench,
        "report": "\n".join(lines),
        "passed": passed
    }


@app.route("/", methods=["GET", "POST"])
def home():
    global last_result
    expression = request.form.get("expression", "(A & B) | (A & C)")
    result, error = None, None

    if request.method == "POST":
        try:
            result = run_project(expression)
            last_result = result
        except Exception as exc:
            error = str(exc)

    return render_template_string(
        PAGE, expression=expression, result=result, error=error
    )


@app.route("/download/<kind>")
def download(kind):
    mapping = {
        "verilog": ("verilog", "optimized_circuit.v"),
        "testbench": ("testbench", "testbench.v"),
        "report": ("report", "simulation_report.txt")
    }
    if kind not in mapping or not last_result:
        return "Run a successful simulation first.", 400

    key, filename = mapping[kind]
    return Response(
        last_result[key],
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
