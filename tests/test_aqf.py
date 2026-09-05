import pytest

from rescan.aqf import map_to_aqf, meets_minimum


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Bachelor of Science", 7),
        ("Bachelor of Engineering (Software) with Honours", 8),
        ("B.Eng (Hons)", 8),
        ("Master of Computer Science", 9),
        ("MSc", 9),
        ("PhD in Computer Science", 10),
        ("Graduate Certificate in Data Science", 8),
        ("Graduate Diploma of Data Science", 8),
        ("Advanced Diploma of IT", 6),
        ("Diploma of Nursing", 5),
        ("Certificate IV in Cyber Security", 4),
        ("Certificate III in Business", 3),
        ("Bachelor of Technology in Computer Science", 7),
        ("LLB", 7),
    ],
)
def test_maps_known_qualifications(title, expected):
    level, label, confidence = map_to_aqf(title)
    assert level == expected
    assert label
    assert confidence > 0


@pytest.mark.parametrize(
    "title",
    ["", None, "Certificate of Completion - Docker", "Nanodegree in AI", "Coursera badge"],
)
def test_non_awards_get_no_level(title):
    level, label, confidence = map_to_aqf(title)
    assert level is None and label is None and confidence == 0.0


def test_masters_beats_bachelor_when_both_words_appear():
    # Ordering matters: "Master" must win over a trailing "Bachelor" mention.
    assert map_to_aqf("Master of Science (following Bachelor of Arts)")[0] == 9


def test_unknown_level_never_silently_passes_a_minimum():
    # Unknown is "not proven", so it must not clear a threshold on its own.
    assert meets_minimum(None, 7) is False
    assert meets_minimum(7, 7) is True
    assert meets_minimum(6, 7) is False


def test_mapping_is_identical_for_equivalent_titles():
    # Two candidates holding the same award must always get the same level.
    assert map_to_aqf("Bachelor of Science")[0] == map_to_aqf("bachelor of science")[0]
