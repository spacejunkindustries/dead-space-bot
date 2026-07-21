"""Deterministic arithmetic for conversation mode (GDD §6.8).

The same posture as the understanding brain (§6.7): the model never gets the
dangerous power. Numbers a pilot asks CORTANA to compute are answered by an
exact, on-box evaluator — never by a language model that would happily
hallucinate ``437 * 12 = 5240``.

The evaluator is a hand-rolled tokenizer → shunting-yard → RPN over
:class:`decimal.Decimal`. It has **no** ``eval``/``exec``/``ast`` and no
name resolution, so no identifier, attribute, call, or dunder can ever be
reached — a stray ``__import__`` or ``os.system`` is a lexer error, not code.
Every runaway (an exponent bomb like ``10**10**10``, a megabyte of digits, a
div-by-zero) raises :class:`MathError` *fast* rather than hanging the box.

``try_calc(text)`` is the entry point the ConversationManager calls first: it
detects a calculation ("what is 5 plus 7", "437*12"), evaluates it, and
returns a spoken-ready string — or ``None`` when the utterance is not a bare
calculation, so ordinary chit-chat falls through to the model.
"""

from __future__ import annotations

import re
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext

__all__ = ["MathError", "evaluate", "try_calc"]


class MathError(Exception):
    """The expression is not a well-formed, safely-evaluable calculation."""


#: Hard input-length cap — a genuine spoken calculation is short; anything
#: longer is prose, not arithmetic, and must not enter the tokenizer.
_MAX_INPUT_LEN = 200
#: Token-count cap — bounds shunting-yard/RPN work regardless of length.
_MAX_TOKENS = 80
#: Exponent magnitude cap — ``2 ** 5000`` is fine, ``10 ** 10**10`` is not.
#: Applied to the RIGHT operand of ``**`` before the power is ever computed.
_MAX_EXPONENT = 1000
#: Intermediate/So-far result size cap: reject any value whose integer part
#: exceeds this many digits, so a digit-doubling chain refuses instantly
#: instead of allocating gigabytes.
_MAX_DIGITS = 5000
#: Decimal working precision — bounded so no single op runs unbounded.
_PRECISION = 60

#: Spoken/typed prefixes that introduce a calculation. Stripped before lexing.
_ASK_PREFIX = re.compile(
    r"^\s*(?:hey\s+cortana[,\s]+)?"
    r"(?:what(?:'|’)?s|what\s+is|whats|calculate|compute|work\s+out|how\s+much\s+is)\s+",
    re.IGNORECASE,
)

#: Word forms Whisper/pilots use for the operators, longest-first so
#: "multiplied by" wins over "by". Applied only inside a calc candidate.
_WORD_OPS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bto\s+the\s+power\s+of\b", re.IGNORECASE), "**"),
    (re.compile(r"\bdivided\s+by\b", re.IGNORECASE), "/"),
    (re.compile(r"\bmultiplied\s+by\b", re.IGNORECASE), "*"),
    (re.compile(r"\bmodulo\b", re.IGNORECASE), "%"),
    (re.compile(r"\bmod\b", re.IGNORECASE), "%"),
    (re.compile(r"\bplus\b", re.IGNORECASE), "+"),
    (re.compile(r"\bminus\b", re.IGNORECASE), "-"),
    (re.compile(r"\btimes\b", re.IGNORECASE), "*"),
    (re.compile(r"\bover\b", re.IGNORECASE), "/"),
)

#: Token grammar: a number (int/decimal, optional exponent), an operator, or a
#: paren. Nothing else lexes — an identifier is an error by construction.
_NUMBER = re.compile(r"\d+(?:\.\d+)?|\.\d+")
_TOKEN = re.compile(r"\s*(\d+(?:\.\d+)?|\.\d+|\*\*|[+\-*/%()])")

#: Binary operators: (precedence, right_associative).
_BINOPS: dict[str, tuple[int, bool]] = {
    "+": (1, False),
    "-": (1, False),
    "*": (2, False),
    "/": (2, False),
    "%": (2, False),
    "**": (3, True),
}


def _tokenize(expr: str) -> list[str]:
    """Split ``expr`` into number/operator/paren tokens, or raise MathError on
    the first character that is not part of the grammar (an identifier char,
    a comma, a stray symbol)."""
    tokens: list[str] = []
    pos = 0
    n = len(expr)
    while pos < n:
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN.match(expr, pos)
        if m is None or m.start(1) != pos:
            # Either nothing matched at pos, or the match skipped a
            # non-grammar character — both mean an illegal token.
            raise MathError(f"unexpected character at {pos}: {expr[pos]!r}")
        tokens.append(m.group(1))
        pos = m.end(1)
        if len(tokens) > _MAX_TOKENS:
            raise MathError("too many tokens")
    if not tokens:
        raise MathError("empty expression")
    return tokens


def _to_rpn(tokens: list[str]) -> list[str]:
    """Shunting-yard: infix tokens → RPN, tracking unary minus/plus.

    A ``-``/``+`` in a prefix position (start, after another operator, or after
    ``(``) becomes the unary marker ``u-`` / ``u+`` (precedence above binary,
    right-associative), so ``-3 + 2`` and ``2 * -3`` parse correctly."""
    output: list[str] = []
    stack: list[str] = []
    prev: str | None = None
    for tok in tokens:
        if _NUMBER.fullmatch(tok):
            output.append(tok)
        elif tok == "(":
            stack.append(tok)
        elif tok == ")":
            while stack and stack[-1] != "(":
                output.append(stack.pop())
            if not stack:
                raise MathError("unbalanced parentheses")
            stack.pop()  # discard the "("
        elif tok in _BINOPS or tok in ("+", "-"):
            unary = tok in ("+", "-") and (prev is None or prev == "(" or _is_operator(prev))
            if unary:
                op = "u" + tok
                # Unary binds tighter than any binary op; right-assoc.
                stack.append(op)
            else:
                prec, right = _BINOPS[tok]
                while stack and stack[-1] != "(":
                    top = stack[-1]
                    top_prec = _op_prec(top)
                    if top_prec > prec or (top_prec == prec and not right):
                        output.append(stack.pop())
                    else:
                        break
                stack.append(tok)
        else:  # pragma: no cover — tokenizer never yields anything else
            raise MathError(f"unexpected token {tok!r}")
        prev = tok
    while stack:
        top = stack.pop()
        if top == "(":
            raise MathError("unbalanced parentheses")
        output.append(top)
    return output


def _is_operator(tok: str) -> bool:
    return tok in _BINOPS or tok in ("u-", "u+")


def _op_prec(tok: str) -> int:
    if tok in ("u-", "u+"):
        return 4  # unary above every binary operator
    return _BINOPS[tok][0]


def _guard_size(value: Decimal) -> Decimal:
    """Reject a result whose integer part is absurdly large (a digit bomb)."""
    if not value.is_finite():
        raise MathError("non-finite result")
    exponent = value.adjusted()  # ~ log10(|value|)
    if exponent > _MAX_DIGITS:
        raise MathError("result too large")
    return value


def _apply_binary(op: str, a: Decimal, b: Decimal) -> Decimal:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            raise MathError("division by zero")
        return a / b
    if op == "%":
        if b == 0:
            raise MathError("modulo by zero")
        return a % b
    if op == "**":
        return _power(a, b)
    raise MathError(f"unknown operator {op!r}")  # pragma: no cover


def _power(a: Decimal, b: Decimal) -> Decimal:
    """``a ** b`` with an exponent-magnitude cap checked BEFORE computing, so an
    exponent bomb refuses instantly instead of allocating."""
    if b != b.to_integral_value():
        # Non-integer exponents (roots) need Decimal.__pow__, which is bounded
        # by context precision — but still gate the magnitude.
        if abs(b) > _MAX_EXPONENT:
            raise MathError("exponent too large")
        try:
            return a**b
        except (InvalidOperation, ValueError) as exc:
            raise MathError(f"cannot raise to power: {exc}") from exc
    if abs(b) > _MAX_EXPONENT:
        raise MathError("exponent too large")
    # Bound the RESULT size up front: |a|'s digit count times |b| is the
    # integer-part length of a**b; refuse before the multiply if it blows up.
    if a not in (Decimal(0), Decimal(1), Decimal(-1)):
        approx_digits = (abs(a).adjusted() + 1) * abs(int(b))
        if approx_digits > _MAX_DIGITS:
            raise MathError("result too large")
    try:
        return a**b
    except (InvalidOperation, DivisionByZero, ValueError) as exc:
        raise MathError(f"cannot raise to power: {exc}") from exc


def _eval_rpn(rpn: list[str]) -> Decimal:
    stack: list[Decimal] = []
    for tok in rpn:
        if _NUMBER.fullmatch(tok):
            try:
                stack.append(Decimal(tok))
            except InvalidOperation as exc:  # pragma: no cover — regex guarantees valid
                raise MathError(f"bad number {tok!r}") from exc
        elif tok in ("u-", "u+"):
            if not stack:
                raise MathError("malformed expression")
            operand = stack.pop()
            stack.append(-operand if tok == "u-" else operand)
        elif tok in _BINOPS:
            if len(stack) < 2:
                raise MathError("malformed expression")
            b = stack.pop()
            a = stack.pop()
            stack.append(_guard_size(_apply_binary(tok, a, b)))
        else:  # pragma: no cover — RPN never carries parens
            raise MathError(f"unexpected token {tok!r}")
    if len(stack) != 1:
        raise MathError("malformed expression")
    return stack[0]


def evaluate(expr: str) -> Decimal:
    """Evaluate a pure arithmetic expression to an exact :class:`Decimal`.

    Raises :class:`MathError` on anything that is not a well-formed calculation
    — an identifier, an attribute access, a call, unbalanced parens, a
    div/mod-by-zero, an over-long input, or a runaway magnitude. Never runs
    ``eval``/``exec`` and never resolves a name, so it cannot execute code."""
    if len(expr) > _MAX_INPUT_LEN:
        raise MathError("expression too long")
    tokens = _tokenize(expr)
    rpn = _to_rpn(tokens)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        # Traps stay on for div-by-zero/invalid so they surface as MathError
        # via the guarded operators above rather than silent NaN/Inf.
        result = _eval_rpn(rpn)
        return _guard_size(+result)  # unary + applies context rounding once


def _looks_like_calc(expr: str) -> bool:
    """A cheap pre-filter: the candidate must contain a digit and at least one
    operator (or paren), and consist ONLY of calc characters. Prose with a lone
    number ("bring 3 ships") is rejected here so it reaches the model."""
    if not any(ch.isdigit() for ch in expr):
        return False
    if not any(op in expr for op in ("+", "-", "*", "/", "%", "(", ")")):
        return False
    return bool(re.fullmatch(r"[0-9.\s+\-*/%()]+", expr))


def _format(value: Decimal) -> str:
    """Render a result for speech: drop a trailing ``.0``, strip needless
    fraction zeros, and cap the fraction so an irrational-ish quotient does not
    read out forty digits over comms."""
    normalized = value.normalize()
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent < -6:
        # Long fractional tail (e.g. 1/3): round to 6 places for speaking.
        normalized = normalized.quantize(Decimal("0.000001")).normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def try_calc(text: str) -> str | None:
    """Detect and answer a calculation; ``None`` when ``text`` is not one.

    Handles an explicit ask ("what is 5 plus 7", "calculate 437 * 12") and a
    bare arithmetic utterance ("(2 + 3) * 4"). Word operators ("plus", "times",
    "divided by", "to the power of") are normalised first. A non-calculation —
    ordinary chit-chat, a question with no operator — returns ``None`` so the
    ConversationManager falls through to the model."""
    if not text or not text.strip():
        return None
    candidate = _ASK_PREFIX.sub("", text.strip())
    # Drop a trailing question mark / period and surrounding filler.
    candidate = candidate.strip().rstrip("?.! ").strip()
    for pattern, repl in _WORD_OPS:
        candidate = pattern.sub(repl, candidate)
    candidate = candidate.strip()
    if not _looks_like_calc(candidate):
        return None
    try:
        result = evaluate(candidate)
    except MathError:
        return None
    return _format(result)
