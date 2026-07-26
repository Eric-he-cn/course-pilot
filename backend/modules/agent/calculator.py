"""安全的算术求值：AST 白名单 + 求值时的资源上限。

AST 遍历挡不住资源耗尽——`2**999999999` 只有 5 个节点，危险全在求值那一刻，
所以指数、位移与中间结果都要在求值时限住。
"""
from __future__ import annotations

import ast
import operator

MAX_EXPRESSION_CHARS = 200
MAX_NODES = 60
MAX_EXPONENT = 64
MAX_RESULT_BITS = 512

_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class CalculationError(ValueError):
    pass


def evaluate(expression: str) -> float | int:
    text = str(expression or "").strip()
    if not text:
        raise CalculationError("表达式不能为空")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise CalculationError(f"表达式不能超过 {MAX_EXPRESSION_CHARS} 字符")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise CalculationError(f"表达式语法错误：{error.msg}") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise CalculationError("表达式过于复杂")
    return _eval(tree.body)


def _eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculationError("只支持数字常量")
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _guard(_UNARY[type(node.op)](_eval(node.operand)))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > MAX_EXPONENT or not float(right).is_integer()):
            raise CalculationError(f"指数只支持不超过 {MAX_EXPONENT} 的整数")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise CalculationError("除数不能为 0")
        try:
            return _guard(_BINARY[type(node.op)](left, right))
        except OverflowError:
            raise CalculationError("计算结果超出范围") from None
    raise CalculationError("只支持 + - * / // % ** 与括号的算术表达式")


def _guard(value: float | int) -> float | int:
    if isinstance(value, int) and value.bit_length() > MAX_RESULT_BITS:
        raise CalculationError("中间结果过大")
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        raise CalculationError("计算结果不是有限数")
    return value
