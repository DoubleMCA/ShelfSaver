"""The languages a library can keep, and the tags files use for each of them."""
from __future__ import annotations

from typing import Any, Optional

DEFAULT_LANGUAGE = "eng"

_LANGUAGES = {
    "eng": ("English", {"eng", "en", "english"}),
    "ara": ("Arabic", {"ara", "ar", "arabic"}),
    "zho": ("Chinese", {"zho", "chi", "zh", "chinese", "cmn", "yue"}),
    "nld": ("Dutch", {"nld", "dut", "nl", "dutch"}),
    "fra": ("French", {"fra", "fre", "fr", "french"}),
    "deu": ("German", {"deu", "ger", "de", "german"}),
    "hin": ("Hindi", {"hin", "hi", "hindi"}),
    "ind": ("Indonesian", {"ind", "id", "indonesian"}),
    "ita": ("Italian", {"ita", "it", "italian"}),
    "jpn": ("Japanese", {"jpn", "ja", "japanese"}),
    "kor": ("Korean", {"kor", "ko", "korean"}),
    "pol": ("Polish", {"pol", "pl", "polish"}),
    "por": ("Portuguese", {"por", "pt", "portuguese"}),
    "rus": ("Russian", {"rus", "ru", "russian"}),
    "spa": ("Spanish", {"spa", "es", "spanish"}),
    "swe": ("Swedish", {"swe", "sv", "swedish"}),
    "tur": ("Turkish", {"tur", "tr", "turkish"}),
}


def primary_language(tag) -> str:
    # Only the main part of the tag counts, so en-US and en_GB are English but enm is not.
    return (tag or "").strip().lower().replace("_", "-").split("-")[0]


def is_language(tag, code: str) -> bool:
    return primary_language(tag) in _LANGUAGES[code][1]


def language_name(code: str) -> str:
    return _LANGUAGES[code][0]


def language_choices() -> list[dict]:
    return [{"code": code, "name": name} for code, (name, _) in _LANGUAGES.items()]


def clean_language(raw: Any) -> str:
    """The language to save. Raises ValueError with a reason to show."""
    if raw not in _LANGUAGES:
        raise ValueError("Choose a language from the list.")
    return raw


def load_language(saved: Optional[str]) -> str:
    """The saved language. A damaged setting must never remove the wrong tracks, so it counts
    as the default."""
    return saved if saved in _LANGUAGES else DEFAULT_LANGUAGE
