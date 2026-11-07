import pytest

from shelfsaver.languages import (
    DEFAULT_LANGUAGE,
    clean_language,
    is_language,
    language_choices,
    language_name,
    load_language,
)


def test_the_default_is_english():
    assert DEFAULT_LANGUAGE == "eng" and language_name(DEFAULT_LANGUAGE) == "English"


def test_the_choices_list_english_first_then_by_name():
    names = [choice["name"] for choice in language_choices()]
    assert names[0] == "English"
    assert names[1:] == sorted(names[1:])


@pytest.mark.parametrize("name", ["Spanish", "Japanese", "Italian", "Hindi", "Chinese", "Indonesian"])
def test_the_choices_include_languages_found_in_the_library(name):
    assert name in [choice["name"] for choice in language_choices()]


@pytest.mark.parametrize("tag,code", [
    ("fre", "fra"), ("fra", "fra"), ("fr", "fra"), ("French", "fra"),
    ("ger", "deu"), ("deu", "deu"), ("de-AT", "deu"),
    ("chi", "zho"), ("zho", "zho"), ("zh_CN", "zho"),
    ("dut", "nld"), ("nld", "nld"),
    ("es-419", "spa"), ("pt-BR", "por"),
])
def test_every_way_of_tagging_a_language_counts(tag, code):
    assert is_language(tag, code)


@pytest.mark.parametrize("tag,code", [("eng", "spa"), ("enm", "eng"), ("", "eng"), (None, "eng")])
def test_other_tags_do_not_count(tag, code):
    assert not is_language(tag, code)


def test_clean_language_accepts_a_listed_language():
    assert clean_language("spa") == "spa"


@pytest.mark.parametrize("raw", ["", "klingon", "SPA", "es"])
def test_clean_language_refuses_anything_else_with_a_reason(raw):
    with pytest.raises(ValueError, match="Choose a language"):
        clean_language(raw)


@pytest.mark.parametrize("saved", [None, "", "klingon"])
def test_a_missing_or_damaged_setting_means_the_default(saved):
    assert load_language(saved) == DEFAULT_LANGUAGE


def test_a_saved_language_is_used():
    assert load_language("jpn") == "jpn"
