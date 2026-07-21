"""The deterministic arithmetic evaluator for conversation mode (GDD §6.8).

Numbers are answered exactly, never by a model — so this evaluator is where
the "the model never gets the dangerous power" posture is enforced. It must be
correct on real arithmetic AND refuse anything that isn't (identifiers,
attribute access, exponent bombs, div-by-zero) FAST, without hanging.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from cortana.chat_math import MathError, evaluate, try_calc

# ── precedence / associativity / operators ───────────────────────────────────


def test_precedence() -> None:
    assert evaluate("2+3*4") == Decimal(14)


def test_parentheses_override_precedence() -> None:
    assert evaluate("(2+3)*4") == Decimal(20)


def test_unary_minus_leading() -> None:
    assert evaluate("-3+2") == Decimal(-1)


def test_unary_minus_after_operator() -> None:
    assert evaluate("2*-3") == Decimal(-6)


def test_exponent_is_right_associative() -> None:
    # 2 ** (3 ** 2) == 2 ** 9 == 512, NOT (2**3)**2 == 64.
    assert evaluate("2**3**2") == Decimal(512)


def test_modulo() -> None:
    assert evaluate("10%3") == Decimal(1)


def test_decimal_arithmetic_is_exact() -> None:
    # The whole point of Decimal over float: 0.1 + 0.2 is exactly 0.3.
    assert evaluate("0.1+0.2") == Decimal("0.3")


def test_nested_parentheses() -> None:
    assert evaluate("((1+2)*(3+4))") == Decimal(21)


# ── refusals (the safety gates) ──────────────────────────────────────────────


def test_division_by_zero_refused() -> None:
    with pytest.raises(MathError):
        evaluate("1/0")


def test_modulo_by_zero_refused() -> None:
    with pytest.raises(MathError):
        evaluate("10%0")


def test_exponent_bomb_refuses_fast() -> None:
    # 10**10**10 would be astronomically large — it must refuse (not compute).
    start = time.monotonic()
    with pytest.raises(MathError):
        evaluate("10**10**10")
    assert time.monotonic() - start < 1.0, "exponent bomb must refuse instantly, not hang"


def test_identifier_rejected() -> None:
    with pytest.raises(MathError):
        evaluate("x+1")


def test_dunder_and_attribute_rejected() -> None:
    for hostile in ("__import__", "os.system", "(1).__class__", "a.b"):
        with pytest.raises(MathError):
            evaluate(hostile)


def test_call_syntax_rejected() -> None:
    with pytest.raises(MathError):
        evaluate("pow(2,3)")


def test_input_length_capped() -> None:
    with pytest.raises(MathError):
        evaluate("1+" * 500 + "1")


def test_unbalanced_parentheses_rejected() -> None:
    with pytest.raises(MathError):
        evaluate("(2+3")
    with pytest.raises(MathError):
        evaluate("2+3)")


def test_empty_expression_rejected() -> None:
    with pytest.raises(MathError):
        evaluate("   ")


# ── try_calc extraction ──────────────────────────────────────────────────────


def test_try_calc_spoken_ask_words() -> None:
    assert try_calc("what is 5 plus 7") == "12"


def test_try_calc_bare_arithmetic() -> None:
    assert try_calc("437*12") == "5244"


def test_try_calc_word_operators() -> None:
    assert try_calc("whats 100 divided by 4") == "25"
    assert try_calc("what is 6 times 7") == "42"
    assert try_calc("calculate 2 to the power of 10") == "1024"


def test_try_calc_strips_trailing_punctuation() -> None:
    assert try_calc("what is 2 + 2?") == "4"


def test_try_calc_non_math_returns_none() -> None:
    assert try_calc("how are you doing today") is None


def test_try_calc_bare_number_is_not_a_calc() -> None:
    # "bring 3 ships" has a number but no operator — it is chit-chat, not maths,
    # and must fall through to the model.
    assert try_calc("bring 3 ships") is None


def test_try_calc_empty_returns_none() -> None:
    assert try_calc("") is None
    assert try_calc("   ") is None


def test_try_calc_hostile_input_returns_none() -> None:
    # A calc-shaped ask over a hostile token never raises out of try_calc —
    # it just declines and the utterance becomes ordinary chat.
    assert try_calc("what is __import__") is None
    assert try_calc("what is 1/0") is None
