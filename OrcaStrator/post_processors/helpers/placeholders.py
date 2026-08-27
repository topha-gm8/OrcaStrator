#!/usr/bin/env python3
# OrcaStrator, a graphical post-processor runner for multi-toolhead 3D printers
# Copyright (C) 2026  Topha_GM8
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Shared placeholder registry + tiny expression language for any processor
that renders a user-authored template string against g-code-derived data
(gcode_template_notice.py today, and any future one).

Two kinds of placeholders, merged into one flat namespace at render time:

  - CONFIG_BLOCK placeholders: every "; key = value" resolved-setting
    comment OrcaSlicer writes into its CONFIG_BLOCK, parsed generically
    by parse_config_block() -- ANY key OrcaSlicer happens to write
    (nozzle_diameter, filament_type, machine_tool_change_time, whatever
    a given profile/printer produces), not a curated subset. This module
    deliberately does NOT maintain a fixed list of "known" config keys --
    the whole point is a user isn't limited to ones we thought to name.
  - Computed placeholders: values derived from the g-code itself rather
    than read off a single comment line (total_number_toolchanges
    today). Declared in COMPUTED_PLACEHOLDERS so the GUI's placeholder
    reference panel and the runtime resolver read off the exact same
    list -- see that dict's own docstring for why that matters.

The expression language (parse_expression/eval_node/render_template) is
intentionally NOT Python eval: a small whitelist grammar (identifiers,
numbers, strings, + - * / // % arithmetic, comparisons, and/or/not, a
C-style ternary, calls into a fixed HELPER_FUNCTIONS dict, and a `let`
assignment -- see render_template's own docstring) with no attribute
access and no way to reach anything outside the namespace it's handed.
This runs on a local desktop tool the user themselves configures, so
this isn't defending against a hostile input -- it's defending against
a typo in someone's own template silently doing something surprising,
and keeping the grammar small enough that the placeholder reference
panel can honestly describe everything a template can do.
"""
import re


# ---------------------------------------------------------------------------
# CONFIG_BLOCK parsing
# ---------------------------------------------------------------------------

# Anchored to "first non-whitespace char on the line is ';'" so this never
# matches a trailing inline comment on a real move/command line (those
# always have g-code before the ';'). OrcaSlicer's CONFIG_BLOCK is exactly
# this shape: one resolved setting per comment line, "key = value".
PAT_CONFIG_KV = re.compile(r'^\s*;\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')


def _coerce(raw: str):
    """Best-effort typing of a CONFIG_BLOCK value: bool, int, float, else
    left as the raw string (many OrcaSlicer settings are semicolon-
    separated per-extruder lists, e.g. "PLA;PETG" -- those stay strings
    and are still perfectly usable in a template, just not arithmetic)."""
    if raw == "":
        return raw
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.match(r'^-?\d+$', raw):
        try:
            return int(raw)
        except ValueError:
            pass
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_config_block(lines) -> dict:
    """Every "; key = value" line in the file, generically -- not scoped
    to a delimited region, matching the pattern already established by
    find_machine_tool_change_time()/find_estimated_print_time_seconds()
    in timeline_scale.py. Last occurrence of a given key wins (matches
    how a simple line-by-line scan naturally behaves; CONFIG_BLOCK keys
    aren't expected to repeat in practice)."""
    result = {}
    for ln in lines:
        m = PAT_CONFIG_KV.match(ln)
        if not m:
            continue
        result[m.group(1)] = _coerce(m.group(2))
    return result


# ---------------------------------------------------------------------------
# Computed placeholders
# ---------------------------------------------------------------------------

# name -> (description, resolver(lines, config_block) -> value)
# This IS the doc source for the GUI's placeholder reference panel -- a new
# computed placeholder is added here once and both the runtime resolver and
# the GUI list pick it up automatically, so the two can never drift apart.
def _resolve_total_number_toolchanges(lines, config_block):
    # Local, deliberately-uncoupled toolchange count (same PAT_TOOLCHANGE
    # every timeline processor already uses) rather than importing
    # toolchange_heatmap.py's own event list -- this module has no business
    # depending on another processor's internals, see CLAUDE.md's
    # decoupling guidance ("post_processors/*.py files are independent").
    pat = re.compile(r'^\s*T(\d+)\b')
    return sum(1 for ln in lines if pat.match(ln.strip()))


def _resolve_estimated_print_time_seconds(lines, config_block):
    from .timeline_scale import find_estimated_print_time_seconds
    return find_estimated_print_time_seconds(lines)


COMPUTED_PLACEHOLDERS = {
    "total_number_toolchanges": (
        "Total count of every T<n> toolchange command in the file, "
        "including the initial tool selection at print start.",
        _resolve_total_number_toolchanges,
    ),
    "estimated_print_time_seconds": (
        "OrcaSlicer's own \"estimated printing time (normal mode)\" line, "
        "converted to whole seconds.",
        _resolve_estimated_print_time_seconds,
    ),
}


def build_namespace(lines) -> dict:
    """The flat name -> value dict a template renders against: every
    CONFIG_BLOCK key (as-is, arbitrary), overlaid with COMPUTED_PLACEHOLDERS
    (a computed name never collides with a real CONFIG_BLOCK key in
    practice, but computed values intentionally win if it ever does --
    a caller wrote real logic for those, a raw comment scrape didn't).
    A resolver that raises is skipped rather than failing the whole
    build -- that placeholder just won't be defined, which surfaces as a
    normal "unknown placeholder" error only if a template actually uses
    it, not as a hard failure for everyone else."""
    namespace = parse_config_block(lines)
    for name, (_desc, resolver) in COMPUTED_PLACEHOLDERS.items():
        try:
            namespace[name] = resolver(lines, namespace)
        except Exception:
            pass
    return namespace


def placeholder_catalog(lines=None) -> list:
    """For the GUI's placeholder reference panel: a list of (name,
    description, category) tuples. If `lines` is given (a real or sample
    g-code file), every CONFIG_BLOCK key actually present is included too
    (category "config_block") -- otherwise only the fixed computed set is
    returned (category "computed"), since CONFIG_BLOCK keys can't be known
    in advance without a real file to read them from."""
    catalog = [(name, desc, "computed") for name, (desc, _r) in COMPUTED_PLACEHOLDERS.items()]
    if lines is not None:
        for key in sorted(parse_config_block(lines).keys()):
            catalog.append((key, "From this file's CONFIG_BLOCK.", "config_block"))
    return catalog


# ---------------------------------------------------------------------------
# Helper functions callable from inside a template expression
# ---------------------------------------------------------------------------

def _fn_time_format(seconds) -> str:
    seconds = max(0, int(round(float(seconds))))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fn_round(x, n=0):
    return round(float(x), int(n))


def _fn_number_format(x, decimals=2) -> str:
    return f"{float(x):.{int(decimals)}f}"


def _fn_pluralize(n, singular, plural=None) -> str:
    plural = singular + "s" if plural is None else plural
    return singular if float(n) == 1 else plural


HELPER_FUNCTIONS = {
    "time_format": _fn_time_format,
    "round": _fn_round,
    "number_format": _fn_number_format,
    "pluralize": _fn_pluralize,
    "int": lambda x: int(x),
    "abs": lambda x: abs(x),
    "max": lambda *args: max(args),
    "min": lambda *args: min(args),
}


# ---------------------------------------------------------------------------
# Expression language: tokenizer -> recursive-descent parser -> AST -> eval
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<number>\d+\.\d+|\d+)
      | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op><=|>=|==|!=|\?|:|=|[-+*/%(),<>])
      | (?P<slashslash>//)
    )
""", re.VERBOSE)

_KEYWORDS = {"and", "or", "not", "let"}


class TemplateExpressionError(Exception):
    pass


def _tokenize(src: str) -> list:
    tokens = []
    i = 0
    n = len(src)
    while i < n:
        if src[i].isspace():
            i += 1
            continue
        m = _TOKEN_RE.match(src, i)
        if not m or m.end() == i:
            raise TemplateExpressionError(f"unexpected character {src[i]!r}")
        i = m.end()
        if m.lastgroup == "number":
            tokens.append(("num", float(m.group("number"))))
        elif m.lastgroup == "string":
            raw = m.group("string")[1:-1]
            tokens.append(("str", raw.encode().decode("unicode_escape")))
        elif m.lastgroup == "ident":
            word = m.group("ident")
            tokens.append((word if word in _KEYWORDS else "ident", word))
        elif m.lastgroup == "slashslash":
            tokens.append(("op", "//"))
        else:
            tokens.append(("op", m.group("op")))
    tokens.append(("eof", None))
    return tokens


class _Parser:
    """Recursive-descent parser. Grammar (loosest-binding first):
    statement := let_stmt | expr
    let_stmt := 'let' IDENT '=' expr
    expr := ternary
    ternary := or_expr ('?' expr ':' expr)?
    or_expr := and_expr ('or' and_expr)*
    and_expr := not_expr ('and' not_expr)*
    not_expr := 'not' not_expr | comparison
    comparison := additive (('=='|'!='|'<'|'>'|'<='|'>=') additive)*
    additive := multiplicative (('+'|'-') multiplicative)*
    multiplicative := unary (('*'|'/'|'//'|'%') unary)*
    unary := ('-'|'+') unary | primary
    primary := NUMBER | STRING | IDENT | IDENT '(' args? ')' | '(' expr ')'

    A `let` only ever appears as the WHOLE content of one {...} block
    (see parse(), which is the only caller of let_stmt -- there's no
    grammar rule that lets `let` nest inside a larger expression, e.g.
    as a call argument). render_template() special-cases the resulting
    ("let", name, value_node) node: it's a statement with a side effect
    on that one call's namespace, not a value, so it renders as nothing
    -- see that function's docstring for the scoping/shadowing rules.
    """

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def _peek(self):
        return self.tokens[self.pos]

    def _next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect_op(self, value):
        kind, val = self._peek()
        if kind == "op" and val == value:
            return self._next()
        raise TemplateExpressionError(f"expected {value!r}")

    def parse(self):
        node = self.let_stmt() if self._peek()[0] == "let" else self.expr()
        if self._peek()[0] != "eof":
            raise TemplateExpressionError(f"unexpected trailing input near {self._peek()[1]!r}")
        return node

    def let_stmt(self):
        self._next()  # consume 'let'
        kind, name = self._peek()
        if kind != "ident":
            raise TemplateExpressionError("expected a variable name after 'let'")
        self._next()
        self._expect_op("=")
        return ("let", name, self.expr())

    def expr(self):
        return self.ternary()

    def ternary(self):
        cond = self.or_expr()
        if self._peek() == ("op", "?"):
            self._next()
            a = self.expr()
            self._expect_op(":")
            b = self.expr()
            return ("ternary", cond, a, b)
        return cond

    def or_expr(self):
        node = self.and_expr()
        while self._peek()[0] == "or":
            self._next()
            node = ("binop", "or", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.not_expr()
        while self._peek()[0] == "and":
            self._next()
            node = ("binop", "and", node, self.not_expr())
        return node

    def not_expr(self):
        if self._peek()[0] == "not":
            self._next()
            return ("unop", "not", self.not_expr())
        return self.comparison()

    def comparison(self):
        node = self.additive()
        while self._peek() in (("op", "=="), ("op", "!="), ("op", "<"), ("op", ">"), ("op", "<="), ("op", ">=")):
            op = self._next()[1]
            node = ("binop", op, node, self.additive())
        return node

    def additive(self):
        node = self.multiplicative()
        while self._peek() in (("op", "+"), ("op", "-")):
            op = self._next()[1]
            node = ("binop", op, node, self.multiplicative())
        return node

    def multiplicative(self):
        node = self.unary()
        while self._peek() in (("op", "*"), ("op", "/"), ("op", "//"), ("op", "%")):
            op = self._next()[1]
            node = ("binop", op, node, self.unary())
        return node

    def unary(self):
        if self._peek() in (("op", "-"), ("op", "+")):
            op = self._next()[1]
            return ("unop", op, self.unary())
        return self.primary()

    def primary(self):
        kind, val = self._peek()
        if kind == "num":
            self._next()
            return ("num", val)
        if kind == "str":
            self._next()
            return ("str", val)
        if kind == "op" and val == "(":
            self._next()
            node = self.expr()
            self._expect_op(")")
            return node
        if kind == "ident":
            self._next()
            if self._peek() == ("op", "("):
                self._next()
                args = []
                if self._peek() != ("op", ")"):
                    args.append(self.expr())
                    while self._peek() == ("op", ","):
                        self._next()
                        args.append(self.expr())
                self._expect_op(")")
                return ("call", val, args)
            return ("var", val)
        raise TemplateExpressionError(f"unexpected token {val!r}")


def parse_expression(src: str):
    return _Parser(_tokenize(src)).parse()


def eval_node(node, namespace: dict, functions: dict = None):
    functions = HELPER_FUNCTIONS if functions is None else functions
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "str":
        return node[1]
    if kind == "var":
        name = node[1]
        if name not in namespace:
            raise TemplateExpressionError(f"unknown placeholder '{name}'")
        return namespace[name]
    if kind == "call":
        name, arg_nodes = node[1], node[2]
        if name not in functions:
            raise TemplateExpressionError(f"unknown function '{name}'")
        args = [eval_node(a, namespace, functions) for a in arg_nodes]
        return functions[name](*args)
    if kind == "unop":
        op, x = node[1], eval_node(node[2], namespace, functions)
        if op == "-":
            return -x
        if op == "+":
            return +x
        if op == "not":
            return not x
        raise TemplateExpressionError(f"unknown unary operator '{op}'")
    if kind == "ternary":
        _, cond, a, b = node
        return eval_node(a, namespace, functions) if eval_node(cond, namespace, functions) else eval_node(b, namespace, functions)
    if kind == "binop":
        op = node[1]
        left = eval_node(node[2], namespace, functions)
        if op == "and":
            return left and eval_node(node[3], namespace, functions)
        if op == "or":
            return left or eval_node(node[3], namespace, functions)
        right = eval_node(node[3], namespace, functions)
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            # Template numeric literals are always parsed as float (see
            # _tokenize) even for something written as a plain integer
            # like "10" -- so "#" * 10 would otherwise hit Python's
            # str.__mul__ head-on with a float repeat count and raise,
            # even though nothing about the template text looks like it
            # should. String-repeat is a legitimate, useful thing for a
            # divider/border template ("#" * 10 -> "##########"), so
            # when either side is a str and the other is a whole-number
            # float, coerce that operand to int to match what the
            # template author obviously means. A genuinely fractional
            # count (3.5) has no sensible repeat-count meaning, so that
            # still raises -- with a clearer message than the default
            # TypeError text would give.
            if isinstance(left, str) or isinstance(right, str):
                if isinstance(right, float) and not isinstance(right, bool) and right.is_integer():
                    right = int(right)
                if isinstance(left, float) and not isinstance(left, bool) and left.is_integer():
                    left = int(left)
                if isinstance(left, str) and not isinstance(right, int):
                    raise TemplateExpressionError(
                        f"can't repeat a string by a non-whole-number count ({right!r})")
                if isinstance(right, str) and not isinstance(left, int):
                    raise TemplateExpressionError(
                        f"can't repeat a string by a non-whole-number count ({left!r})")
            return left * right
        if op == "/":
            return left / right
        if op == "//":
            return left // right
        if op == "%":
            return left % right
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == ">=":
            return left >= right
        raise TemplateExpressionError(f"unknown operator '{op}'")
    raise TemplateExpressionError(f"unknown node kind '{kind}'")


def _stringify(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def render_template(text: str, namespace: dict, functions: dict = None):
    """Renders `text`, substituting every {expr} with its evaluated,
    stringified result. {{ and }} escape to literal braces. Never raises --
    a broken expression renders as <ERR:...> inline instead, and is also
    reported back in the returned errors list so a caller can surface a
    warning NOTICE without losing the rest of the template. Returns
    (rendered_text, errors).

    A block can also be a `let` statement instead of an expression --
    {let x = machine_tool_change_time * total_number_toolchanges} -- which
    stores its value under that name and renders as nothing, so a value
    used more than once in one template only has to be computed once.
    Rules, all a consequence of the same one-line implementation (a
    per-call COPY of `namespace`, updated in place as blocks are scanned
    left to right):
      - visible to every {...} block AFTER it in this same template, not
        to ones before it -- there's no hoisting, same as reading it;
      - scoped to this one render_template() call only -- the caller's
        own `namespace` dict is never mutated, so one template's `let`s
        can never leak into another template's rendering, or into a
        second call against the same namespace;
      - a name that collides with a real placeholder shadows it for the
        rest of this render, ordinary dict-assignment semantics, nothing
        special-cased;
      - `let` itself is a reserved word (see _KEYWORDS) -- a CONFIG_BLOCK
        key that happened to be named exactly "let" (none are, in
        practice) would be unreachable as a bare placeholder."""
    functions = HELPER_FUNCTIONS if functions is None else functions
    local_ns = dict(namespace)
    out = []
    errors = []
    i, n = 0, len(text)
    while i < n:
        if text[i:i + 2] == "{{":
            out.append("{")
            i += 2
            continue
        if text[i:i + 2] == "}}":
            out.append("}")
            i += 2
            continue
        if text[i] == "{":
            j = text.find("}", i + 1)
            if j == -1:
                errors.append("unterminated '{' - missing closing '}'")
                out.append(text[i:])
                break
            expr_src = text[i + 1:j]
            try:
                node = parse_expression(expr_src)
                if node[0] == "let":
                    _, name, value_node = node
                    local_ns[name] = eval_node(value_node, local_ns, functions)
                else:
                    value = eval_node(node, local_ns, functions)
                    out.append(_stringify(value))
            except ZeroDivisionError:
                errors.append(f"{{{expr_src}}}: division by zero")
                out.append(f"<ERR:{expr_src}>")
            except TemplateExpressionError as exc:
                errors.append(f"{{{expr_src}}}: {exc}")
                out.append(f"<ERR:{expr_src}>")
            except Exception as exc:  # noqa: BLE001 -- any other eval failure still shouldn't crash the render
                errors.append(f"{{{expr_src}}}: {exc}")
                out.append(f"<ERR:{expr_src}>")
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out), errors


def evaluate_condition(expr_src: str, namespace: dict, functions: dict = None):
    """Evaluates a bare boolean-ish expression -- e.g. "bed_type != 'PEI'"
    -- for a template's optional "condition" gate (see gcode_template_
    notice.py's "condition" field). Same grammar and namespace as a
    {expr} block, just without the surrounding braces and returning the
    raw evaluated truthiness instead of a stringified render.

    Blank/whitespace-only source means "no condition" and always passes
    -- (True, None) -- matching "no templates configured" elsewhere in
    this codebase's "off means off, on means on" opt-in spirit.

    Never raises. A `let` isn't valid here (it has no value of its own
    to test, and a condition is evaluated in isolation, not as part of
    a template's left-to-right block sequence, so it wouldn't have
    anything to be reused by). Any failure -- unknown placeholder, bad
    expression, a `let`, division by zero, whatever -- returns
    (False, error_message): a broken condition fails CLOSED, i.e. it
    just never fires, same "a template problem is never itself the
    thing that stops a print" spirit render_template() already has for
    {expr} blocks. That matters even more here, since a condition can
    gate an "abort" destination -- a typo should never be the reason a
    print gets refused."""
    functions = HELPER_FUNCTIONS if functions is None else functions
    expr_src = expr_src.strip()
    if not expr_src:
        return True, None
    try:
        node = parse_expression(expr_src)
        if node[0] == "let":
            raise TemplateExpressionError("a condition can't be a 'let' assignment")
        return bool(eval_node(node, namespace, functions)), None
    except ZeroDivisionError:
        return False, f"{expr_src}: division by zero"
    except TemplateExpressionError as exc:
        return False, f"{expr_src}: {exc}"
    except Exception as exc:  # noqa: BLE001 -- any other eval failure still shouldn't crash the caller
        return False, f"{expr_src}: {exc}"


def resolve_namespace(lines):
    """Convenience wrapper: build_namespace() plus the raw config_block
    dict, for a caller that wants both (e.g. a debug dump)."""
    config_block = parse_config_block(lines)
    namespace = dict(config_block)
    for name, (_desc, resolver) in COMPUTED_PLACEHOLDERS.items():
        try:
            namespace[name] = resolver(lines, config_block)
        except Exception:
            pass
    return namespace, config_block
