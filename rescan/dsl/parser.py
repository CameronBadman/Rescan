"""Tokenizer and recursive-descent parser for the rule language.

Hand-written rather than generated so the error messages can be specific: a
forbidden field is reported with its legal basis, a type error names the
operators the field does support, and every error carries a position.

Clause boundaries are the REQUIRE / PREFER keywords (or a semicolon), so an
expression may span lines freely.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from rescan.dsl import fields as vocab
from rescan.dsl.ast import (
    Aggregate,
    And,
    AnyRecord,
    Ask,
    Clause,
    Compare,
    Expr,
    HasAll,
    HasAny,
    In,
    Is,
    Not,
    Or,
    Program,
)

KEYWORDS = {
    "REQUIRE", "PREFER", "WEIGHT", "BECAUSE", "AND", "OR", "NOT",
    "HAS", "ANY", "ALL", "IN", "IS", "WHERE", "TRUE", "FALSE", "ASK",
    "COUNT", "SUM", "MAX", "MIN", "AVG",
}
AGGREGATE_KEYWORDS = {"COUNT", "SUM", "MAX", "MIN", "AVG"}

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class DslError(ValueError):
    kind = "error"

    def __init__(self, message: str, line: int = 0, col: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col

    def __str__(self) -> str:
        where = f" (line {self.line}, col {self.col})" if self.line else ""
        return f"{self.message}{where}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "line": self.line, "col": self.col}


class DslSyntaxError(DslError):
    kind = "syntax"


class DslTypeError(DslError):
    kind = "type"


class DslFieldError(DslError):
    """Unknown or forbidden identifier. Forbidden carries the legal basis."""

    kind = "field"

    def __init__(
        self,
        message: str,
        *,
        field: str,
        forbidden: vocab.ForbiddenField | None = None,
        suggestion: str | None = None,
        line: int = 0,
        col: int = 0,
    ) -> None:
        super().__init__(message, line, col)
        self.field = field
        self.forbidden = forbidden
        self.suggestion = suggestion

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["field"] = self.field
        data["forbidden"] = self.forbidden is not None
        if self.forbidden:
            data["reason"] = self.forbidden.reason
            data["statutes"] = self.forbidden.citations()
            data["alternative"] = self.forbidden.alternative
        if self.suggestion:
            data["suggestion"] = self.suggestion
        return data


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    type: str  # KEYWORD | IDENT | NUMBER | STRING | OP | LPAREN | RPAREN | COMMA | SEMI | EOF
    value: Any
    line: int
    col: int


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>[ \t\r\n]+)
  | (?P<comment>--[^\n]*)
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>>=|<=|!=|>|<|=)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<semi>;)
  | (?P<dot>\.)
    """,
    re.VERBOSE,
)


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    line = 1
    line_start = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            col = pos - line_start + 1
            snippet = text[pos : pos + 12].split("\n")[0]
            if text[pos] == '"':
                raise DslSyntaxError("Unterminated string literal", line, col)
            raise DslSyntaxError(f"Unexpected character {snippet[:1]!r}", line, col)
        kind = match.lastgroup
        raw = match.group(0)
        col = pos - line_start + 1
        if kind == "ws":
            newlines = raw.count("\n")
            if newlines:
                line += newlines
                line_start = pos + raw.rfind("\n") + 1
        elif kind == "comment":
            pass
        elif kind == "string":
            body = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            tokens.append(Token("STRING", body, line, col))
        elif kind == "number":
            tokens.append(Token("NUMBER", float(raw), line, col))
        elif kind == "ident":
            upper = raw.upper()
            if upper in KEYWORDS:
                tokens.append(Token("KEYWORD", upper, line, col))
            else:
                tokens.append(Token("IDENT", raw, line, col))
        elif kind == "op":
            tokens.append(Token("OP", raw, line, col))
        elif kind == "lparen":
            tokens.append(Token("LPAREN", raw, line, col))
        elif kind == "rparen":
            tokens.append(Token("RPAREN", raw, line, col))
        elif kind == "comma":
            tokens.append(Token("COMMA", raw, line, col))
        elif kind == "semi":
            tokens.append(Token("SEMI", raw, line, col))
        elif kind == "dot":
            tokens.append(Token("DOT", raw, line, col))
        pos = match.end()
    tokens.append(Token("EOF", None, line, len(text) - line_start + 1))
    return tokens


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


_KIND_OPERATORS: dict[str, set[str]] = {
    "number": {">=", "<=", ">", "<", "=", "!="},
    "text": {"=", "!=", "HAS ANY", "HAS ALL"},
    "enum": {"IS", "IN", "=", "!="},
    "bool": {"IS"},
    "list": {"HAS ANY", "HAS ALL"},
}


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = tokenize(text)
        self.index = 0

    # -- token helpers -----------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def _advance(self) -> Token:
        token = self.current
        self.index += 1
        return token

    def _is_keyword(self, *names: str) -> bool:
        return self.current.type == "KEYWORD" and self.current.value in names

    def _expect_keyword(self, name: str) -> Token:
        if not self._is_keyword(name):
            raise self._unexpected(f"expected {name}")
        return self._advance()

    def _expect(self, token_type: str, description: str) -> Token:
        if self.current.type != token_type:
            raise self._unexpected(f"expected {description}")
        return self._advance()

    def _unexpected(self, expectation: str) -> DslSyntaxError:
        token = self.current
        if token.type == "EOF":
            found = "end of input"
        elif token.type == "STRING":
            found = f'string "{token.value}"'
        else:
            found = repr(token.value if not isinstance(token.value, float) else f"{token.value:g}")
        return DslSyntaxError(f"Unexpected {found}; {expectation}", token.line, token.col)

    # -- grammar -----------------------------------------------------------

    def program(self) -> Program:
        clauses: list[Clause] = []
        while True:
            while self.current.type == "SEMI":
                self._advance()
            if self.current.type == "EOF":
                break
            if not self._is_keyword("REQUIRE", "PREFER"):
                raise self._unexpected("each rule must start with REQUIRE or PREFER")
            clauses.append(self.clause())
        if not clauses:
            raise DslSyntaxError("Empty program: expected at least one REQUIRE or PREFER clause", 1, 1)
        return Program(clauses=clauses)

    def clause(self) -> Clause:
        kind = self._advance().value.lower()
        expr = self.expr()
        weight = 1.0
        because = None
        if self._is_keyword("WEIGHT"):
            token = self._advance()
            if kind != "prefer":
                raise DslSyntaxError("WEIGHT only applies to PREFER clauses", token.line, token.col)
            number = self._expect("NUMBER", "a number after WEIGHT")
            if number.value <= 0:
                raise DslSyntaxError("WEIGHT must be positive", number.line, number.col)
            weight = float(number.value)
        if self._is_keyword("BECAUSE"):
            self._advance()
            because = self._expect("STRING", "a quoted reason after BECAUSE").value
        if self.current.type not in {"SEMI", "EOF"} and not self._is_keyword("REQUIRE", "PREFER"):
            raise self._unexpected("expected the end of the clause, AND, or OR")
        return Clause(kind=kind, expr=expr, weight=weight, because=because)

    def expr(self, record: vocab.RecordSpec | None = None) -> Expr:
        args = [self.and_expr(record)]
        while self._is_keyword("OR"):
            self._advance()
            args.append(self.and_expr(record))
        return args[0] if len(args) == 1 else Or(args=args)

    def and_expr(self, record: vocab.RecordSpec | None) -> Expr:
        args = [self.not_expr(record)]
        while self._is_keyword("AND"):
            self._advance()
            args.append(self.not_expr(record))
        return args[0] if len(args) == 1 else And(args=args)

    def not_expr(self, record: vocab.RecordSpec | None) -> Expr:
        if self._is_keyword("NOT"):
            self._advance()
            return Not(arg=self.not_expr(record))
        return self.primary(record)

    def primary(self, record: vocab.RecordSpec | None) -> Expr:
        token = self.current
        if token.type == "LPAREN":
            self._advance()
            inner = self.expr(record)
            self._expect("RPAREN", "a closing parenthesis")
            return inner
        if self._is_keyword("ANY"):
            if record is not None:
                raise DslSyntaxError("ANY cannot be nested inside a WHERE clause", token.line, token.col)
            return self.any_record()
        if token.type == "KEYWORD" and token.value in AGGREGATE_KEYWORDS:
            if record is not None:
                raise DslSyntaxError("Aggregates cannot be nested inside a WHERE clause", token.line, token.col)
            return self.aggregate()
        if self._is_keyword("ASK"):
            if record is not None:
                raise DslSyntaxError("ASK cannot be used inside a WHERE clause", token.line, token.col)
            self._advance()
            question = self._expect("STRING", "a quoted question after ASK").value.strip()
            if not question:
                raise DslSyntaxError("ASK needs a non-empty question", token.line, token.col)
            return Ask(question=question)
        if token.type == "IDENT":
            return self.comparison(record)
        if token.type == "KEYWORD":
            raise self._unexpected("expected a field name, ANY, ASK, COUNT/SUM/MAX/MIN/AVG, NOT or an opening parenthesis")
        raise self._unexpected("expected a field name")

    def any_record(self) -> AnyRecord:
        self._expect_keyword("ANY")
        name_token = self._expect("IDENT", "a record name after ANY (skill, role or qualification)")
        record = vocab.resolve_record(name_token.value)
        if record is None:
            raise DslFieldError(
                f"Unknown record {name_token.value!r}; ANY quantifies over skill, role or qualification",
                field=name_token.value, line=name_token.line, col=name_token.col,
            )
        self._expect_keyword("WHERE")
        where = self.expr(record)
        return AnyRecord(record=record.name, where=where)

    def aggregate(self) -> Aggregate:
        func_token = self._advance()
        func = func_token.value.lower()
        self._expect("LPAREN", f"an opening parenthesis after {func_token.value}")
        name_token = self._expect("IDENT", f"a record name inside {func_token.value}(...) (skill, role, qualification or project)")
        record = vocab.resolve_record(name_token.value)
        if record is None:
            raise DslFieldError(
                f"Unknown record {name_token.value!r}; aggregates run over skill, role, qualification or project",
                field=name_token.value, line=name_token.line, col=name_token.col,
            )
        field_spec: vocab.FieldSpec | None = None
        if self.current.type == "DOT":
            self._advance()
            field_token = self._expect("IDENT", f"a field name after {record.name}.")
            field_spec = self._resolve(field_token, record)
            if field_spec.kind != "number":
                raise DslTypeError(
                    f"{func_token.value} needs a numeric field; {record.name}.{field_spec.name} is {field_spec.kind}",
                    field_token.line, field_token.col,
                )
        if func == "count" and field_spec is not None:
            raise DslTypeError("COUNT takes a record, not a field: COUNT(role WHERE ...)", func_token.line, func_token.col)
        if func != "count" and field_spec is None:
            numeric = ", ".join(f"{record.name}.{n}" for n, f in record.fields.items() if f.kind == "number")
            raise DslTypeError(
                f"{func_token.value} needs a numeric field, e.g. {numeric or record.name + '.<field>'}",
                func_token.line, func_token.col,
            )
        where = None
        if self._is_keyword("WHERE"):
            self._advance()
            where = self.expr(record)
        self._expect("RPAREN", f"a closing parenthesis after the {func_token.value} arguments")
        cmp_token = self.current
        if cmp_token.type != "OP":
            raise self._unexpected(f"expected a comparison (>=, <=, >, <, =, !=) after {func_token.value}(...)")
        self._advance()
        number = self._expect("NUMBER", f"a number to compare {func_token.value}(...) with")
        return Aggregate(
            func=func, record=record.name, field=field_spec.name if field_spec else None,
            where=where, cmp=cmp_token.value, value=float(number.value),
        )

    def _resolve(self, token: Token, record: vocab.RecordSpec | None) -> vocab.FieldSpec:
        name = token.value
        if record is not None:
            spec = record.fields.get(name.lower())
            if spec is None:
                banned = vocab.forbidden(name)
                if banned:
                    raise self._forbidden(banned, token)
                options = sorted(record.fields)
                raise DslFieldError(
                    f"{name!r} is not a field of {record.name}; inside ANY {record.name} WHERE "
                    f"the fields are {', '.join(options)}",
                    field=name, suggestion=_nearest(name, options), line=token.line, col=token.col,
                )
            return spec
        spec = vocab.resolve_field(name)
        if spec is not None:
            return spec
        banned = vocab.forbidden(name)
        if banned:
            raise self._forbidden(banned, token)
        options = sorted(vocab.FIELDS)
        suggestion = _nearest(name, options + sorted(vocab.ALIASES))
        hint = f"; did you mean {suggestion!r}?" if suggestion else ""
        raise DslFieldError(
            f"Unknown field {name!r}{hint} There are {len(options)} queryable fields "
            f"(e.g. years_experience, aqf, skills, role_titles, has_degree) plus ANY/COUNT over "
            "skill, role, qualification and project; see the language reference.",
            field=name, suggestion=suggestion, line=token.line, col=token.col,
        )

    @staticmethod
    def _forbidden(banned: vocab.ForbiddenField, token: Token) -> DslFieldError:
        citations = "; ".join(banned.citations())
        return DslFieldError(
            f"{banned.name!r} is not queryable: {banned.reason} ({citations}). Instead: {banned.alternative}",
            field=banned.name, forbidden=banned, line=token.line, col=token.col,
        )

    def _check_operator(self, spec: vocab.FieldSpec, operator: str, token: Token) -> None:
        allowed = _KIND_OPERATORS[spec.kind]
        if operator not in allowed:
            raise DslTypeError(
                f"{operator} cannot be used on {spec.name!r} ({spec.kind}); "
                f"use {', '.join(sorted(allowed))}",
                token.line, token.col,
            )

    def comparison(self, record: vocab.RecordSpec | None) -> Expr:
        field_token = self._advance()
        spec = self._resolve(field_token, record)
        token = self.current

        if token.type == "OP":
            self._advance()
            self._check_operator(spec, token.value, token)
            value_token = self.current
            if spec.kind == "number":
                number = self._expect("NUMBER", f"a number to compare {spec.name!r} with")
                return Compare(field=spec.name, cmp=token.value, value=float(number.value))
            if spec.kind == "enum":
                if value_token.type == "IDENT":
                    raw = self._advance().value
                elif value_token.type == "KEYWORD" and value_token.value in {"TRUE", "FALSE"}:
                    raw = self._advance().value.lower()
                else:
                    raw = self._expect("STRING", f"a value for {spec.name!r}").value
                return Compare(field=spec.name, cmp=token.value, value=self._enum_value(spec, raw, value_token))
            text = self._expect("STRING", f"a quoted string to compare {spec.name!r} with")
            return Compare(field=spec.name, cmp=token.value, value=text.value)

        if self._is_keyword("HAS"):
            self._advance()
            if not self._is_keyword("ANY", "ALL"):
                raise self._unexpected("expected ANY or ALL after HAS")
            mode = self._advance().value
            self._check_operator(spec, f"HAS {mode}", token)
            values = self._string_list(f"HAS {mode}")
            if mode == "ANY":
                return HasAny(field=spec.name, values=values)
            return HasAll(field=spec.name, values=values)

        if self._is_keyword("IN"):
            self._advance()
            self._check_operator(spec, "IN", token)
            values = [self._enum_value(spec, v, token) for v in self._string_list("IN")]
            return In(field=spec.name, values=values)

        if self._is_keyword("IS"):
            self._advance()
            self._check_operator(spec, "IS", token)
            value_token = self.current
            if spec.kind == "bool":
                if not self._is_keyword("TRUE", "FALSE"):
                    raise self._unexpected(f"expected TRUE or FALSE after {spec.name} IS")
                return Is(field=spec.name, value=self._advance().value == "TRUE")
            if value_token.type == "IDENT":
                raw = self._advance().value
            elif value_token.type == "STRING":
                raw = self._advance().value
            else:
                raise self._unexpected(f"expected one of {', '.join(spec.enum_values)} after {spec.name} IS")
            return Is(field=spec.name, value=self._enum_value(spec, raw, value_token))

        if token.type == "KEYWORD" and token.value in {"AND", "OR"} or token.type in {"RPAREN", "EOF", "SEMI"}:
            raise self._unexpected(
                f"a field must be followed by an operator ({_KIND_OPERATORS[spec.kind] and ', '.join(sorted(_KIND_OPERATORS[spec.kind]))})"
            )
        raise self._unexpected(f"expected an operator after {spec.name!r}")

    def _enum_value(self, spec: vocab.FieldSpec, raw: str, token: Token) -> str:
        value = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if value not in spec.enum_values:
            raise DslTypeError(
                f"{raw!r} is not a value of {spec.name!r}; use one of {', '.join(spec.enum_values)}",
                token.line, token.col,
            )
        return value

    def _string_list(self, operator: str) -> list[str]:
        self._expect("LPAREN", f"an opening parenthesis after {operator}")
        values: list[str] = []
        while True:
            token = self._expect("STRING", f"a quoted value inside {operator} (...)")
            if not token.value.strip():
                raise DslSyntaxError(f"Empty value inside {operator} (...)", token.line, token.col)
            values.append(token.value.strip())
            if self.current.type == "COMMA":
                self._advance()
                continue
            break
        self._expect("RPAREN", f"a closing parenthesis after the {operator} values")
        return values


def _nearest(name: str, options: list[str]) -> str | None:
    matches = difflib.get_close_matches(name.lower(), options, n=1, cutoff=0.6)
    return matches[0] if matches else None


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def parse_program(text: str) -> Program:
    """Parse one or more REQUIRE / PREFER clauses."""
    return _Parser(text).program()


def parse_expr(text: str) -> Expr:
    """Parse a bare expression, as used by ad-hoc queries."""
    parser = _Parser(text)
    if parser._is_keyword("REQUIRE", "PREFER"):
        token = parser.current
        raise DslSyntaxError(
            "A query is a bare expression; REQUIRE / PREFER belong in a rule program",
            token.line, token.col,
        )
    if parser.current.type == "EOF":
        raise DslSyntaxError("Empty expression", 1, 1)
    expr = parser.expr()
    if parser.current.type != "EOF":
        raise parser._unexpected("expected the end of the expression")
    return expr


def parse_clause(text: str) -> Clause:
    """Parse exactly one clause; a bare expression is treated as REQUIRE."""
    stripped = text.strip()
    if not stripped:
        raise DslSyntaxError("Empty rule", 1, 1)
    if not re.match(r"(?i)^(require|prefer)\b", stripped):
        stripped = "REQUIRE " + stripped
    program = parse_program(stripped)
    if len(program.clauses) != 1:
        raise DslSyntaxError(f"Expected one clause, found {len(program.clauses)}", 1, 1)
    return program.clauses[0]
