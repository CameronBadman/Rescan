import json

import pytest

from rescan.mcp_server import server


def unwrap(result):
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


async def test_all_tools_are_exposed_with_descriptions():
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {
        "check_screening_rule",
        "check_screening_rules",
        "list_known_risky_phrases",
        "map_qualification_to_aqf",
    } <= set(tools)
    for tool in tools.values():
        assert tool.description and len(tool.description) > 40


async def test_risky_rule_returns_statute_and_rewrite():
    result = unwrap(
        await server.call_tool("check_screening_rule", {"rule_text": "Must be a native English speaker"})
    )
    assert result["verdict"] == "risky"
    assert result["applied_automatically"] is False
    finding = result["findings"][0]
    assert any("Racial Discrimination Act" in s for s in finding["statutes"])
    assert "native" not in finding["suggested_rewrite"].lower()


async def test_sound_rule_returns_the_compiled_test():
    result = unwrap(
        await server.call_tool("check_screening_rule", {"rule_text": "At least 5 years of Python experience"})
    )
    assert result["verdict"] == "applicable"
    assert result["test"]["dsl"].startswith("REQUIRE years_experience >= 5")
    assert "Python" in result["test"]["dsl"]
    assert result["test"]["clause"]["kind"] == "require"


async def test_batch_check_counts_applied_and_flagged():
    result = unwrap(
        await server.call_tool(
            "check_screening_rules",
            {"rules": ["Recent graduates only", "Bachelor degree required"]},
        )
    )
    assert result["applied"] == 1
    assert result["flagged"] == 1
    assert len(result["results"]) == 2


async def test_known_phrase_list_carries_statutes():
    result = unwrap(await server.call_tool("list_known_risky_phrases", {}))
    assert len(result["patterns"]) >= 16
    assert all(p["statutes"] and p["suggested_rewrite"] for p in result["patterns"])
    assert "not legal advice" in result["disclaimer"]


@pytest.mark.parametrize(
    "title,level",
    [("B.Eng (Hons)", 8), ("Bachelor of Science", 7), ("PhD", 10), ("Diploma of Nursing", 5)],
)
async def test_aqf_mapping_tool(title, level):
    result = unwrap(await server.call_tool("map_qualification_to_aqf", {"title": title}))
    assert result["aqf_level"] == level
    assert result["needs_human_check"] is False


async def test_unmappable_qualification_asks_for_a_human_rather_than_failing():
    result = unwrap(await server.call_tool("map_qualification_to_aqf", {"title": "Nanodegree in AI"}))
    assert result["aqf_level"] is None
    assert result["needs_human_check"] is True
