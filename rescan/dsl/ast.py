"""Syntax tree for the rule language.

Nodes are Pydantic models so a compiled program serialises into the job record
and the audit trail as-is, and so a model can also emit the tree directly under
guided decoding. `to_dsl()` renders the canonical text, which is what a
reviewer sees.
"""

from __future__ import annotations

from typing import Annotated, Iterator, Literal, Union

from pydantic import BaseModel, Field

Comparator = Literal[">=", "<=", ">", "<", "=", "!="]


def quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _number(value: float) -> str:
    return f"{value:g}"


class Compare(BaseModel):
    op: Literal["compare"] = "compare"
    field: str
    cmp: Comparator
    value: float | str

    def to_dsl(self) -> str:
        rendered = quote(self.value) if isinstance(self.value, str) else _number(self.value)
        return f"{self.field} {self.cmp} {rendered}"


class HasAny(BaseModel):
    op: Literal["has_any"] = "has_any"
    field: str
    values: list[str]

    def to_dsl(self) -> str:
        return f"{self.field} HAS ANY ({', '.join(quote(v) for v in self.values)})"


class HasAll(BaseModel):
    op: Literal["has_all"] = "has_all"
    field: str
    values: list[str]

    def to_dsl(self) -> str:
        return f"{self.field} HAS ALL ({', '.join(quote(v) for v in self.values)})"


class In(BaseModel):
    op: Literal["in"] = "in"
    field: str
    values: list[str]

    def to_dsl(self) -> str:
        return f"{self.field} IN ({', '.join(quote(v) for v in self.values)})"


class Is(BaseModel):
    op: Literal["is"] = "is"
    field: str
    value: bool | str

    def to_dsl(self) -> str:
        rendered = ("TRUE" if self.value else "FALSE") if isinstance(self.value, bool) else self.value
        return f"{self.field} IS {rendered}"


class Ask(BaseModel):
    """A check the model makes itself, from the anonymized profile only."""

    op: Literal["ask"] = "ask"
    question: str

    def to_dsl(self) -> str:
        return f"ASK {quote(self.question)}"


AggregateFunc = Literal["count", "sum", "max", "min", "avg"]


class Aggregate(BaseModel):
    """COUNT/SUM/MAX/MIN/AVG over a record list, compared with a number.

    `field` is None for COUNT. `where` filters the records first.
    """

    op: Literal["aggregate"] = "aggregate"
    func: AggregateFunc
    record: str
    field: str | None = None
    where: "Expr | None" = None
    cmp: Comparator
    value: float

    def subject(self) -> str:
        target = self.record if self.field is None else f"{self.record}.{self.field}"
        where = f" WHERE {self.where.to_dsl()}" if self.where is not None else ""
        return f"{self.func.upper()}({target}{where})"

    def to_dsl(self) -> str:
        return f"{self.subject()} {self.cmp} {_number(self.value)}"


class Not(BaseModel):
    op: Literal["not"] = "not"
    arg: "Expr"

    def to_dsl(self) -> str:
        inner = self.arg.to_dsl()
        if isinstance(self.arg, (And, Or)):
            inner = f"({inner})"
        return f"NOT {inner}"


class And(BaseModel):
    op: Literal["and"] = "and"
    args: list["Expr"]

    def to_dsl(self) -> str:
        parts = [f"({a.to_dsl()})" if isinstance(a, Or) else a.to_dsl() for a in self.args]
        return " AND ".join(parts)


class Or(BaseModel):
    op: Literal["or"] = "or"
    args: list["Expr"]

    def to_dsl(self) -> str:
        return " OR ".join(a.to_dsl() for a in self.args)


class AnyRecord(BaseModel):
    op: Literal["any"] = "any"
    record: str
    where: "Expr"

    def to_dsl(self) -> str:
        return f"ANY {self.record} WHERE {self.where.to_dsl()}"


Expr = Annotated[
    Union[Compare, HasAny, HasAll, In, Is, Ask, Aggregate, Not, And, Or, AnyRecord],
    Field(discriminator="op"),
]

Not.model_rebuild()
And.model_rebuild()
Or.model_rebuild()
AnyRecord.model_rebuild()
Aggregate.model_rebuild()


class Clause(BaseModel):
    kind: Literal["require", "prefer"] = "require"
    expr: Expr
    weight: float = 1.0
    because: str | None = None

    def to_dsl(self) -> str:
        text = f"{self.kind.upper()} {self.expr.to_dsl()}"
        if self.kind == "prefer" and self.weight != 1.0:
            text += f" WEIGHT {_number(self.weight)}"
        if self.because:
            text += f" BECAUSE {quote(self.because)}"
        return text


class Program(BaseModel):
    clauses: list[Clause] = Field(default_factory=list)

    def to_dsl(self) -> str:
        return "\n".join(clause.to_dsl() for clause in self.clauses)


# --------------------------------------------------------------------------
# Walking
# --------------------------------------------------------------------------


def iter_nodes(expr: Expr) -> Iterator[Expr]:
    yield expr
    if isinstance(expr, Not):
        yield from iter_nodes(expr.arg)
    elif isinstance(expr, (And, Or)):
        for arg in expr.args:
            yield from iter_nodes(arg)
    elif isinstance(expr, AnyRecord):
        yield from iter_nodes(expr.where)
    elif isinstance(expr, Aggregate) and expr.where is not None:
        yield from iter_nodes(expr.where)


def string_literals(expr: Expr) -> list[str]:
    """Every quoted string in the expression, including ASK questions."""
    found: list[str] = []
    for node in iter_nodes(expr):
        if isinstance(node, Compare) and isinstance(node.value, str):
            found.append(node.value)
        elif isinstance(node, (HasAny, HasAll, In)):
            found.extend(node.values)
        elif isinstance(node, Is) and isinstance(node.value, str):
            found.append(node.value)
        elif isinstance(node, Ask):
            found.append(node.question)
    return found


def fields_used(expr: Expr) -> list[str]:
    """Field names touched, as `field` or `record.field`, in order of first use."""
    seen: list[str] = []

    def visit(node: Expr, prefix: str) -> None:
        if isinstance(node, (Compare, HasAny, HasAll, In, Is)):
            name = f"{prefix}{node.field}"
            if name not in seen:
                seen.append(name)
        elif isinstance(node, Ask):
            if "model_check" not in seen:
                seen.append("model_check")
        elif isinstance(node, Not):
            visit(node.arg, prefix)
        elif isinstance(node, (And, Or)):
            for arg in node.args:
                visit(arg, prefix)
        elif isinstance(node, AnyRecord):
            visit(node.where, f"{node.record}.")
        elif isinstance(node, Aggregate):
            name = f"{node.func}({node.record}{'.' + node.field if node.field else ''})"
            if name not in seen:
                seen.append(name)
            if node.where is not None:
                visit(node.where, f"{node.record}.")

    visit(expr, "")
    return seen


def has_ask(expr: Expr) -> bool:
    return any(isinstance(node, Ask) for node in iter_nodes(expr))
