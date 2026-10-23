import itertools
import re
import subprocess
import tempfile
from html import escape
from pathlib import Path

import sympy as sp
from sympy.logic.boolalg import And, Or, Not, Xor, simplify_logic
from flask import Flask, request, render_template_string, Response
from markupsafe import Markup

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
<div class="card">
<h2>1. Original Circuit Diagram</h2>
<p>Gate blocks are labeled AND, OR, NOT or XOR. Inputs and final output F are labeled.</p>
{{ result.original_diagram }}
<h2>2. Optimized Circuit Diagram</h2>
{{ result.optimized_diagram }}
<p style="font-size:13px;color:#475569">The diagram shows the Boolean expression structure. A simplified single-variable expression may need only a direct connection.</p>
</div>
<div class="card">
<h2>3. Digital Waveform</h2>
<p>Each step is one truth-table combination. High level = 1; low level = 0.</p>
{{ result.waveform }}
<p style="font-size:13px;color:#475569">This is a truth-table timing view with equal-duration steps, not physical gate-delay analysis.</p>
</div>
<div class="card">
<h2>4. Karnaugh Map (K-map)</h2>
{% if result.kmap %}
<p>Green cells contain 1. G labels show which simplified term covers a cell.</p>
{{ result.kmap }}
{% else %}
<p>K-map is displayed for expressions with 2, 3 or 4 variables. Current expression has {{ result.variable_count }} variable(s).</p>
{% endif %}
<p style="font-size:13px;color:#475569">Rows and columns use Gray-code ordering for 3- and 4-variable maps.</p>
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



def make_circuit_svg(expr, title):
    # Expression-tree schematic: every logic block is explicitly named.
    leaves = []
    def walk(node):
        if getattr(node, "is_Symbol", False) or not getattr(node, "args", ()):
            leaves.append(node)
        else:
            for child in node.args:
                walk(child)
    walk(expr)
    if not leaves:
        leaves = [expr]
    positions = {}
    for i, node in enumerate(leaves):
        positions.setdefault(str(node), 45 + i * 48)
    height = max(120, 75 + (len(leaves) - 1) * 48)
    def depth(node):
        if getattr(node, "is_Symbol", False) or not getattr(node, "args", ()):
            return 0
        return 1 + max(depth(c) for c in node.args)
    width = max(360, 145 + (depth(expr) + 1) * 105)
    out = [f'<svg style="width:100%;min-width:320px" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape(title)}">',
           '<rect x="1" y="1" width="99%" height="98%" rx="8" fill="#fbfdff" stroke="#d5deea"/>',
           f'<text x="12" y="20" font-size="12" font-weight="bold" fill="#172033">{escape(title)}</text>']
    leaf_i = [0]
    def draw(node, level=0):
        if getattr(node, "is_Symbol", False) or not getattr(node, "args", ()):
            y = positions.get(str(node), 45 + leaf_i[0] * 48)
            leaf_i[0] += 1
            out.append(f'<text x="20" y="{y+4}" font-size="13" fill="#172033">{escape(str(node))}</text>')
            return (35, y)
        children = [draw(c, level + 1) for c in node.args]
        y = sum(c[1] for c in children) / len(children)
        x = width - 100 - level * 95
        gate = "AND" if isinstance(node, And) else "OR" if isinstance(node, Or) else "NOT" if isinstance(node, Not) else "XOR" if isinstance(node, Xor) else "GATE"
        for i, (cx, cy) in enumerate(children):
            iy = y if len(children) == 1 else y - 10 + i * 20 / (len(children) - 1)
            out.append(f'<path d="M {cx} {cy} H {x-10} V {iy} H {x}" fill="none" stroke="#334155" stroke-width="1.5"/>')
        out.append(f'<rect x="{x}" y="{y-17}" width="62" height="34" rx="5" fill="#e4efff" stroke="#3676bd"/>')
        out.append(f'<text x="{x+31}" y="{y+4}" text-anchor="middle" font-size="10" font-weight="bold" fill="#17477e">{gate}</text>')
        return (x + 62, y)
    ox, oy = draw(expr)
    out.append(f'<path d="M {ox} {oy} H {width-25}" stroke="#334155" stroke-width="1.5"/>')
    out.append(f'<text x="{width-22}" y="{oy-7}" font-size="13" font-weight="bold" fill="#172033">F</text></svg>')
    return Markup("".join(out))


def make_waveform(names, rows):
    left, top, step, lane = 55, 20, 34, 38
    signals = list(names) + ["F"]
    width = left + len(rows) * step + 12
    height = top + len(signals) * lane + 10
    out = [f'<svg style="width:100%;min-width:320px" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Digital waveform">',
           '<rect width="100%" height="100%" rx="8" fill="#fbfdff" stroke="#d5deea"/>']
    for li, signal in enumerate(signals):
        y0 = top + li * lane
        high, low = y0 + 5, y0 + 24
        out.append(f'<text x="7" y="{y0+18}" font-size="12" font-weight="bold" fill="#172033">{escape(signal)}</text>')
        vals = [r[signal] for r in rows]
        path = []
        for i, value in enumerate(vals):
            x1, x2 = left + i*step, left + (i+1)*step
            y = high if value else low
            if i == 0:
                path.append(f'M {x1} {y}')
            else:
                prev = high if vals[i-1] else low
                path.append(f'L {x1} {prev} L {x1} {y}')
            path.append(f'L {x2} {y}')
        out.append(f'<path d="{" ".join(path)}" fill="none" stroke="#1673c7" stroke-width="2"/>')
        out.append(f'<line x1="{left}" y1="{y0+31}" x2="{width-4}" y2="{y0+31}" stroke="#e2e8f0"/>')
    out.append('</svg>')
    return Markup("".join(out))


def make_kmap(names, rows, optimized):
    n = len(names)
    if n not in (2, 3, 4):
        return None
    gray2 = ["00", "01", "11", "10"]
    if n == 2:
        row_codes, col_codes = ["0", "1"], ["0", "1"]
    elif n == 3:
        row_codes, col_codes = ["0", "1"], gray2
    else:
        row_codes, col_codes = gray2, gray2
    values = {"".join(str(r[v]) for v in names): r["F"] for r in rows}
    sop = simplify_logic(optimized, form="dnf", force=True)
    terms = [sop] if not isinstance(sop, Or) else list(sop.args)
    groups = {}
    for gi, term in enumerate(terms, 1):
        for bits in values:
            assignment = {sp.Symbol(name): bool(int(bit)) for name, bit in zip(names, bits)}
            try:
                if bool(term.subs(assignment)):
                    groups.setdefault(bits, []).append(f"G{gi}")
            except Exception:
                pass
    html = ['<table style="border-collapse:collapse;margin:12px auto;text-align:center"><tr><th style="padding:9px;border:1px solid #9aa8bb">Row / Col</th>']
    for code in col_codes:
        html.append(f'<th style="padding:9px;border:1px solid #9aa8bb;background:#edf2f8">{code}</th>')
    html.append('</tr>')
    for rc in row_codes:
        html.append(f'<tr><th style="padding:9px;border:1px solid #9aa8bb;background:#edf2f8">{rc}</th>')
        for cc in col_codes:
            bits = rc + cc
            val = values.get(bits, 0)
            group_names = ", ".join(groups.get(bits, []))
            bg = "#d9fbe5" if val else "#f7f8fa"
            extra = f'<small style="display:block;color:#075985">{escape(group_names)}</small>' if val and group_names else ""
            html.append(f'<td style="min-width:35px;padding:10px;border:2px solid {"#16834a" if val else "#cbd5e1"};background:{bg}">{val}{extra}</td>')
        html.append('</tr>')
    html.append('</table>')
    html.append('<p><b>Group labels:</b> ' + ' &nbsp; '.join(
        f'<span style="color:#075985">G{i}: {escape(str(term))}</span>'
        for i, term in enumerate(terms, 1)
    ) + '</p>')
    return Markup("".join(html))


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
    truth_rows = []
    for bits, actual in matches:
        row = {name: int(bit) for name, bit in zip(names, bits)}
        row["F"] = int(actual)
        truth_rows.append(row)
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
        "passed": passed,
        "original_diagram": make_circuit_svg(original, "Original circuit"),
        "optimized_diagram": make_circuit_svg(optimized, "Optimized circuit"),
        "waveform": make_waveform(names, truth_rows),
        "kmap": make_kmap(names, truth_rows, optimized),
        "variable_count": len(names)
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
