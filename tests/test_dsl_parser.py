import pytest

from rescan.dsl import (
    DslError,
    DslFieldError,
    DslSyntaxError,
    DslTypeError,
    parse_clause,
    parse_expr,
    parse_program,
)
from rescan.dsl.ast import Aggregate, And, AnyRecord, Ask, Compare, HasAll, HasAny, Is, Not, Or, fields_used, string_literals
from rescan.dsl.fields import EXAMPLES, FIELDS, FORBIDDEN, RECORDS, reference, reference_text


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_every_documented_example_parses_and_round_trips():
    program = parse_program(EXAMPLES)
    assert len(program.clauses) == len(EXAMPLES.strip().splitlines())
    for clause in program.clauses:
        assert parse_program(clause.to_dsl()).clauses[0] == clause


def test_precedence_is_not_then_and_then_or():
    expr = parse_expr('NOT aqf >= 7 AND years_experience >= 8 OR skills HAS ANY ("Go")')
    assert isinstance(expr, Or)
    assert isinstance(expr.args[0], And)
    assert isinstance(expr.args[0].args[0], Not)
    assert isinstance(expr.args[0].args[0].arg, Compare)


def test_parentheses_override_precedence_and_survive_rendering():
    expr = parse_expr('aqf >= 7 AND (years_experience >= 8 OR skills HAS ANY ("Go"))')
    assert isinstance(expr, And)
    assert isinstance(expr.args[1], Or)
    assert expr.to_dsl() == 'aqf >= 7 AND (years_experience >= 8 OR skills HAS ANY ("Go"))'
    assert parse_expr(expr.to_dsl()) == expr


def test_keywords_and_field_names_are_case_insensitive():
    assert parse_expr("Years_Experience >= 5 and AQF >= 7") == parse_expr("years_experience >= 5 AND aqf >= 7")


def test_aliases_canonicalise():
    expr = parse_expr("total_years_experience >= 5")
    assert isinstance(expr, Compare) and expr.field == "years_experience"
    assert expr.to_dsl() == "years_experience >= 5"


def test_comments_and_multi_line_expressions_are_fine():
    program = parse_program(
        """
        -- hard requirements
        REQUIRE years_experience >= 5
            AND skills HAS ALL ("Python", "SQL")  -- both
        PREFER aqf >= 9 WEIGHT 2
        """
    )
    assert [c.kind for c in program.clauses] == ["require", "prefer"]
    assert program.clauses[1].weight == 2


def test_semicolons_separate_clauses_too():
    program = parse_program("REQUIRE aqf >= 7; REQUIRE years_experience >= 2;")
    assert len(program.clauses) == 2


def test_string_escapes_round_trip():
    expr = parse_expr(r'skills HAS ANY ("C++", "say \"hi\"", "back\\slash")')
    assert isinstance(expr, HasAny)
    assert expr.values == ["C++", 'say "hi"', "back\\slash"]
    assert parse_expr(expr.to_dsl()) == expr


def test_any_record_where_uses_record_fields():
    expr = parse_expr('ANY skill WHERE name = "Python" AND years >= 3')
    assert isinstance(expr, AnyRecord) and expr.record == "skill"
    assert fields_used(expr) == ["skill.name", "skill.years"]


def test_record_aliases_resolve():
    expr = parse_expr('ANY jobs WHERE title HAS ANY ("engineer")')
    assert isinstance(expr, AnyRecord) and expr.record == "role"


def test_aggregates_parse_with_and_without_filters():
    count = parse_expr('COUNT(role WHERE seniority IN ("senior", "lead")) >= 1')
    assert isinstance(count, Aggregate) and count.func == "count" and count.field is None
    total = parse_expr("SUM(role.months) >= 24")
    assert isinstance(total, Aggregate) and total.field == "months" and total.where is None
    assert parse_expr(count.to_dsl()) == count
    assert fields_used(count) == ["count(role)", "role.seniority"]


def test_ask_is_a_primary_that_composes():
    expr = parse_expr('skills HAS ANY ("Kafka") OR ASK "Has the candidate built streaming pipelines?"')
    assert isinstance(expr, Or) and isinstance(expr.args[1], Ask)
    assert "Has the candidate built streaming pipelines?" in string_literals(expr)


def test_because_and_weight_are_kept():
    clause = parse_clause('PREFER people_managed_max >= 5 WEIGHT 2.5 BECAUSE "Leads a squad."')
    assert clause.kind == "prefer" and clause.weight == 2.5 and clause.because == "Leads a squad."
    assert clause.to_dsl() == 'PREFER people_managed_max >= 5 WEIGHT 2.5 BECAUSE "Leads a squad."'


def test_parse_clause_accepts_a_bare_expression_as_require():
    assert parse_clause("aqf >= 7").kind == "require"


def test_bool_and_enum_forms():
    assert isinstance(parse_expr("has_degree IS TRUE"), Is)
    assert parse_expr("work_rights IS unrestricted").value == "unrestricted"
    assert parse_expr('work_rights IN ("citizen", "permanent_resident")').values == ["citizen", "permanent_resident"]
    assert parse_expr('work_rights = "Permanent Resident"').value == "permanent_resident"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["region", "suburb", "institution", "completion_year", "nationality", "summary", "age"])
def test_forbidden_fields_fail_at_parse_time_with_a_statute(name):
    with pytest.raises(DslFieldError) as excinfo:
        parse_expr(f'{name} = "x"')
    error = excinfo.value
    assert error.forbidden is not None
    payload = error.to_dict()
    assert payload["forbidden"] is True
    assert payload["statutes"], "a forbidden field must cite its legal basis"
    assert payload["alternative"]
    assert "not queryable" in str(error)


def test_forbidden_fields_are_also_caught_inside_where():
    with pytest.raises(DslFieldError) as excinfo:
        parse_expr('ANY role WHERE start = "2020"')
    assert excinfo.value.forbidden is not None


def test_unknown_field_suggests_the_nearest_name():
    with pytest.raises(DslFieldError) as excinfo:
        parse_expr("year_experience >= 5")
    assert excinfo.value.forbidden is None
    assert excinfo.value.suggestion == "years_experience"


@pytest.mark.parametrize(
    "text",
    [
        "skills >= 3",
        'aqf HAS ANY ("x")',
        "has_degree >= 1",
        "work_rights IS martian",
        'COUNT(role.months) >= 2',
        "SUM(role) >= 2",
        "AVG(skill.name) >= 2",
        'ANY role WHERE title >= 3',
    ],
)
def test_operator_field_mismatches_are_type_errors(text):
    with pytest.raises(DslTypeError):
        parse_expr(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "years_experience >=",
        'years_experience >= "five"',
        "aqf >= 7 OR",
        "(aqf >= 7",
        'skills HAS ("Go")',
        'skills HAS ANY ()',
        'ASK "x" AND',
        "REQUIRE aqf >= 7",  # bare expressions only in parse_expr
        'ANY role WHERE ANY skill WHERE name = "x"',
        'ANY role WHERE ASK "nested?"',
    ],
)
def test_syntax_errors_carry_a_position(text):
    with pytest.raises(DslSyntaxError) as excinfo:
        parse_expr(text)
    assert excinfo.value.line >= 1 and excinfo.value.col >= 1


def test_program_requires_require_or_prefer():
    with pytest.raises(DslSyntaxError):
        parse_program("aqf >= 7")


def test_weight_is_only_for_prefer():
    with pytest.raises(DslSyntaxError):
        parse_program("REQUIRE aqf >= 7 WEIGHT 2")


def test_error_reports_line_and_column_on_later_lines():
    with pytest.raises(DslError) as excinfo:
        parse_program('REQUIRE aqf >= 7\nREQUIRE region = "x"')
    assert excinfo.value.line == 2


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


def test_vocabulary_is_large_and_every_field_has_a_description():
    assert len(FIELDS) >= 60
    assert len(RECORDS) == 4
    assert sum(len(r.fields) for r in RECORDS.values()) >= 20
    assert len(FORBIDDEN) >= 40
    for spec in FIELDS.values():
        assert spec.description and spec.label
    for banned in FORBIDDEN.values():
        assert banned.citations(), f"{banned.name} cites no statute"


def test_no_forbidden_name_is_also_a_field():
    assert not set(FORBIDDEN) & set(FIELDS)


def test_reference_lists_everything_and_reads_as_prompt_text():
    ref = reference()
    assert ref["counts"]["fields"] == len(FIELDS)
    assert {f["name"] for f in ref["fields"]} == set(FIELDS)
    text = reference_text()
    assert "FORBIDDEN IDENTIFIERS" in text and "GRAMMAR" in text and "EXAMPLES" in text
    for spec in FIELDS.values():
        assert spec.name in text


def test_employer_is_queryable():
    expr = parse_expr('ANY role WHERE employer = "Atlassian" AND months >= 12')
    assert fields_used(expr) == ["role.employer", "role.months"]
    assert parse_expr('companies HAS ANY ("Atlassian")').to_dsl() == 'employers HAS ANY ("Atlassian")'
