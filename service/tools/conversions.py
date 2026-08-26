"""Arithmetic and unit conversion, computed rather than generated.

WHY THESE ARE TOOLS AND NOT JUST THE MODEL ANSWERING
----------------------------------------------------
The 2026-08-18 diagnosis put it plainly: with no conversion or calculator tool,
"what's 20% of X" either went to `run_shell` (a calculator via bash) or came
straight out of the model's own arithmetic — "the exact 'confidently wrong
number' failure mode the whole tool-based design exists to avoid elsewhere."

A wrong number is the worst kind of wrong answer, because nothing about it looks
wrong. Every other tool in this registry exists to stop the model inventing
facts; arithmetic deserves the same treatment, and it is the cheapest possible
case to get exactly right.

SAFETY OF `calculate`
---------------------
The expression is parsed with `ast` and walked node by node against an
allow-list. It is NOT `eval`. `eval` on model-generated text is arbitrary code
execution with extra steps — `__import__('os').system(...)` is a valid Python
expression — and no amount of regex pre-filtering makes that safe. Only literal
numbers, the arithmetic operators, and a fixed set of math functions can appear;
anything else is a parse-time refusal, before evaluation.
"""
from __future__ import annotations

import ast
import math
import operator
import re

import httpx

from service.tools.registry import register

# --------------------------------------------------------------------------
# calculate
# --------------------------------------------------------------------------
_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "floor": math.floor,
    "ceil": math.ceil, "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "min": min, "max": max, "sum": sum, "pow": pow,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

# Exponentiation is the one operator here that can burn CPU and memory without
# any large-looking input: 9**9**9 is three characters of "work" and an
# effectively unbounded integer. Bound the operands rather than the result,
# because by the time there is a result it is already too late.
_MAX_POW_BASE = 1e6
_MAX_POW_EXP = 1000


class _BadExpression(ValueError):
    pass


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _BadExpression(f"{node.value!r} is not a number")
        return node.value
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise _BadExpression("that operator isn't allowed")
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(left) > _MAX_POW_BASE or abs(right) > _MAX_POW_EXP:
                raise _BadExpression("that power is too large to compute")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise _BadExpression("division by zero")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise _BadExpression("that operator isn't allowed")
        return op(_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            name = getattr(node.func, "id", "that function")
            raise _BadExpression(f"{name} isn't available")
        if node.keywords:
            raise _BadExpression("keyword arguments aren't supported")
        return _FUNCS[node.func.id](*(_eval(a) for a in node.args))
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise _BadExpression(f"{node.id!r} isn't a known constant")
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]  # type: ignore[return-value]
    raise _BadExpression("that isn't a plain arithmetic expression")


def _tidy(value: float) -> str:
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        if abs(value - round(value)) < 1e-12 and abs(value) < 1e15:
            return f"{int(round(value)):,}"
        return f"{round(value, 10):,}".rstrip("0").rstrip(".")
    return f"{value:,}" if isinstance(value, int) else str(value)


# "20% of 250", "15 percent of 80" — the single most common shape of this
# request, and not valid Python until it is rewritten.
_PCT_OF_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+([\d.,]+)", re.I)
_PCT_BARE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# People say "15 times 32" as often as "15*32", and the model relays it
# verbatim. Rewritten BEFORE parsing rather than taught to the parser, so the
# allow-list walk still only ever sees ordinary arithmetic.
#
# "divided by" and "multiplied by" come first: the alternation is ordered
# longest-first so "divided" can't match on its own and strand the "by".
_WORD_OPS = {
    "divided by": "/", "multiplied by": "*", "times": "*", "plus": "+",
    "minus": "-", "over": "/", "x": "*",
}
_WORD_OPS_RE = re.compile(
    r"\b(?:divided\s+by|multiplied\s+by|times|plus|minus|over|x)\b", re.I)


@register(
    "calculate",
    "Do arithmetic exactly. Use this for ANY calculation — percentages, tips, "
    "totals, splitting a bill, square roots — instead of working the number out "
    "yourself, which risks a confidently wrong answer. Accepts ordinary "
    "expressions ('1234*0.15', '(89+45)/3', 'sqrt(2)') and percentage phrasing "
    "('20% of 250').",
    {
        "type": "object",
        "properties": {
            "expression": {"type": "string",
                           "description": "The calculation, e.g. '18% of 64.50' or '(120+35)*3'."},
        },
        "required": ["expression"],
    },
    category="compute",
    aliases=["what's 20 percent of 250", "split 87 dollars four ways",
             "how much is a 20% tip on 64.50", "what's 15 times 32",
             "add these up for me", "what's the square root of 180"],
)
def calculate(expression: str) -> str:
    raw = (expression or "").strip()
    if not raw:
        return "(error: calculate needs an `expression`.)"

    expr = raw.rstrip("=?").strip()
    expr = _PCT_OF_RE.sub(lambda m: f"({m.group(1)}/100)*({m.group(2).replace(',', '')})", expr)
    expr = _PCT_BARE_RE.sub(lambda m: f"({m.group(1)}/100)", expr)
    expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**")
    expr = _WORD_OPS_RE.sub(lambda m: _WORD_OPS[m.group(0).lower()], expr)
    expr = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expr)   # thousands separators

    if len(expr) > 500:
        return "(error: that expression is too long.)"
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return (f"(error: I couldn't read {raw!r} as a calculation. "
                f"Try something like '18% of 64.50' or '(120+35)*3'.)")
    try:
        value = _eval(tree)
    except _BadExpression as e:
        return f"(error: {e}.)"
    except (ValueError, OverflowError, ZeroDivisionError) as e:
        return f"(error: {e}.)"
    if isinstance(value, list):
        return ", ".join(_tidy(v) for v in value)
    return _tidy(value)


# --------------------------------------------------------------------------
# convert_units
# --------------------------------------------------------------------------
# Everything is defined against one base unit per dimension, so a conversion is
# always (value * factor_from) / factor_to. Temperature is the exception —
# it is affine, not linear — and is handled separately below.
_LENGTH = {"mm": .001, "cm": .01, "m": 1.0, "km": 1000.0,
           "in": .0254, "inch": .0254, "inches": .0254, "ft": .3048,
           "foot": .3048, "feet": .3048, "yd": .9144, "yard": .9144,
           "yards": .9144, "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
           "nmi": 1852.0}
_MASS = {"mg": 1e-6, "g": .001, "kg": 1.0, "t": 1000.0, "tonne": 1000.0,
         "oz": .028349523125, "ounce": .028349523125, "ounces": .028349523125,
         "lb": .45359237, "lbs": .45359237, "pound": .45359237,
         "pounds": .45359237, "stone": 6.35029318, "st": 6.35029318}
_VOLUME = {"ml": .001, "l": 1.0, "liter": 1.0, "litre": 1.0, "liters": 1.0,
           "litres": 1.0, "tsp": .00492892159375, "tbsp": .01478676478125,
           "cup": .2365882365, "cups": .2365882365,
           "floz": .0295735295625, "pt": .473176473, "pint": .473176473,
           "qt": .946352946, "quart": .946352946,
           "gal": 3.785411784, "gallon": 3.785411784, "gallons": 3.785411784}
_TIME = {"ms": .001, "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
         "min": 60.0, "minute": 60.0, "minutes": 60.0, "h": 3600.0,
         "hr": 3600.0, "hour": 3600.0, "hours": 3600.0, "day": 86400.0,
         "days": 86400.0, "week": 604800.0, "weeks": 604800.0}
_DATA = {"b": 1.0, "byte": 1.0, "bytes": 1.0, "kb": 1e3, "mb": 1e6, "gb": 1e9,
         "tb": 1e12, "kib": 1024.0, "mib": 1024.0 ** 2, "gib": 1024.0 ** 3,
         "tib": 1024.0 ** 4, "bit": .125, "bits": .125}
_SPEED = {"mps": 1.0, "m/s": 1.0, "kmh": 1 / 3.6, "km/h": 1 / 3.6,
          "kph": 1 / 3.6, "mph": .44704, "knot": .514444, "knots": .514444}

_DIMENSIONS = {"length": _LENGTH, "mass": _MASS, "volume": _VOLUME,
               "time": _TIME, "data": _DATA, "speed": _SPEED}
_TEMPS = {"c", "celsius", "f", "fahrenheit", "k", "kelvin"}


def _to_celsius(v: float, unit: str) -> float:
    if unit in ("c", "celsius"):
        return v
    if unit in ("f", "fahrenheit"):
        return (v - 32) * 5 / 9
    return v - 273.15


def _from_celsius(c: float, unit: str) -> float:
    if unit in ("c", "celsius"):
        return c
    if unit in ("f", "fahrenheit"):
        return c * 9 / 5 + 32
    return c + 273.15


@register(
    "convert_units",
    "Convert a measurement between units — length, weight, volume, "
    "temperature, speed, time, or data size. Use this rather than converting "
    "in your head. Example: value=180, from_unit='lb', to_unit='kg'.",
    {
        "type": "object",
        "properties": {
            "value": {"type": "number", "description": "The number to convert."},
            "from_unit": {"type": "string", "description": "Unit to convert FROM, e.g. 'lb', 'miles', 'F', 'GB'."},
            "to_unit": {"type": "string", "description": "Unit to convert TO, e.g. 'kg', 'km', 'C', 'MB'."},
        },
        "required": ["value", "from_unit", "to_unit"],
    },
    category="compute",
    aliases=["how many kilometers is a marathon", "what's 180 pounds in kilos",
             "convert 350 fahrenheit to celsius", "how many cups in a litre",
             "is 30 degrees hot in fahrenheit", "how many gigabytes is that"],
)
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    src = (from_unit or "").strip().lower().replace("°", "").replace("degrees", "").strip()
    dst = (to_unit or "").strip().lower().replace("°", "").replace("degrees", "").strip()
    if not src or not dst:
        return "(error: convert_units needs both `from_unit` and `to_unit`.)"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return f"(error: {value!r} isn't a number.)"

    if src in _TEMPS or dst in _TEMPS:
        if src not in _TEMPS or dst not in _TEMPS:
            return (f"(error: can't convert between {from_unit!r} and {to_unit!r} — "
                    f"one is a temperature and the other isn't.)")
        out = _from_celsius(_to_celsius(value, src), dst)
        return f"{_tidy(value)}°{src[0].upper()} = {_tidy(round(out, 4))}°{dst[0].upper()}"

    for dim, table in _DIMENSIONS.items():
        if src in table and dst in table:
            out = value * table[src] / table[dst]
            return f"{_tidy(value)} {from_unit.strip()} = {_tidy(round(out, 10))} {to_unit.strip()}"

    known = {u for t in _DIMENSIONS.values() for u in t} | _TEMPS
    if src not in known:
        return f"(error: I don't know the unit {from_unit!r}.)"
    if dst not in known:
        return f"(error: I don't know the unit {to_unit!r}.)"
    return (f"(error: {from_unit!r} and {to_unit!r} measure different things, "
            f"so they can't be converted.)")


@register(
    "convert_currency",
    "Convert between currencies at today's exchange rate. Use this rather "
    "than convert_units, which has no live rate — currency moves daily and "
    "guessing a rate from training data is exactly the 'confidently wrong "
    "number' this tool exists to prevent.",
    {
        "type": "object",
        "properties": {
            "amount": {"type": "number", "description": "Amount to convert."},
            "from_currency": {"type": "string", "description": "3-letter currency code, e.g. 'USD'."},
            "to_currency": {"type": "string", "description": "3-letter currency code, e.g. 'EUR'."},
        },
        "required": ["amount", "from_currency", "to_currency"],
    },
    category="web_read",
    aliases=["how much is 100 dollars in euros", "convert 50 pounds to usd",
             "what's the exchange rate for yen right now",
             "how many dollars is 200 canadian"],
)
async def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    src = (from_currency or "").strip().upper()
    dst = (to_currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", src) or not re.fullmatch(r"[A-Z]{3}", dst):
        return "(error: currency codes must be 3 letters, e.g. USD, EUR, GBP.)"
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return f"(error: {amount!r} isn't a number.)"

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.frankfurter.dev/v1/latest",
                            params={"base": src, "symbols": dst})
    except httpx.HTTPError as e:
        return f"(could not reach the exchange-rate service: {e})"
    if r.status_code >= 400:
        return f"(error: {src!r} or {dst!r} isn't a recognized currency code.)"
    data = r.json()
    rate = (data.get("rates") or {}).get(dst)
    if rate is None:
        return f"(error: no rate found for {src} -> {dst}.)"
    out = amount * rate
    return (f"{_tidy(amount)} {src} = {_tidy(round(out, 2))} {dst} "
            f"(rate: 1 {src} = {rate} {dst}, as of {data.get('date', 'today')})")
