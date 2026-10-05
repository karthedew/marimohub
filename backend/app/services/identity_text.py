"""What usernames, Display Names and other text people send about themselves may hold.

An Owner chooses whom to add to a Workspace by the username and Display Name
the person search shows (docs/adr/0004), and registration is open. So a new
username must not pass for someone else's, and neither text may change how
the page around it renders, as an unterminated right-to-left override would.
PostgreSQL text cannot hold NUL either: text carrying one must fail
validation with a 422, not the query with a 500.

Characters are named by code point, never written literally: an invisible
character in this file would be as hard to review as in a username.
"""

import unicodedata

_CONTROL = "Cc"  # NUL, newlines, tabs, BEL, ...
# Never in a name: controls; invisible formatting (zero-width characters, and
# bidi controls such as U+202E, which reverses the text after it); lone
# surrogates, which cannot be stored; and line and paragraph separators.
_NOT_IN_A_NAME = frozenset({_CONTROL, "Cf", "Cs", "Zl", "Zp"})
# A Display Name may keep the zero-width non-joiner (U+200C) and joiner
# (U+200D): Persian and Indic names and emoji sequences need them, and they
# only join or part the characters around them.
_NAME_JOINERS = frozenset({chr(0x200C), chr(0x200D)})
# Characters that render as nothing yet are letters or marks, so neither
# `_NOT_IN_A_NAME` nor `str.isprintable` refuses them. With the two blocks of
# variation selectors, they are the rest of Unicode's Default_Ignorable_Code_Point.
_INVISIBLE_LETTERS_AND_MARKS = frozenset(
    map(
        chr,
        [
            0x034F,  # combining grapheme joiner
            0x115F,  # Hangul choseong filler
            0x1160,  # Hangul jungseong filler
            0x3164,  # Hangul filler
            0xFFA0,  # halfwidth Hangul filler
            0x17B4,  # Khmer vowel inherent AQ
            0x17B5,  # Khmer vowel inherent AA
            0x180B,  # Mongolian free variation selector one
            0x180C,  # Mongolian free variation selector two
            0x180D,  # Mongolian free variation selector three
            0x180F,  # Mongolian free variation selector four
        ],
    )
)
_VARIATION_SELECTORS = (range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0))


def has_control_character(text: str) -> bool:
    """Return whether ``text`` holds a control character (Unicode category Cc), NUL included."""
    return any(unicodedata.category(char) == _CONTROL for char in text)


def check_display_name(name: str) -> str:
    """Return ``name`` if it can be a Display Name, else raise `ValueError` saying why.

    A Display Name is not unique, so it need not look unlike anyone else's;
    it only must not break or reorder what is shown around it.
    """
    if any(
        unicodedata.category(char) in _NOT_IN_A_NAME and char not in _NAME_JOINERS for char in name
    ):
        raise ValueError(
            "a name must not contain control characters, line breaks or invisible formatting"
        )
    return name


def check_username(username: str) -> str:
    """Return ``username`` if a new account may take it, else raise `ValueError` saying why.

    A username is what tells two people with the same Display Name apart,
    so it must look like no other: nothing invisible, no space at either end,
    and only characters NFKC leaves alone, which rules out look-alike
    compatibility forms such as full-width letters and ligatures.
    """
    if not username.isprintable() or any(_is_invisible(char) for char in username):
        raise ValueError("a username must not contain control, invisible or formatting characters")
    if username.strip() != username:
        raise ValueError("a username must not start or end with a space")
    if unicodedata.normalize("NFKC", username) != username:
        raise ValueError(
            "a username must not contain look-alike forms such as full-width letters, "
            "ligatures or separately composed accents"
        )
    return username


def _is_invisible(char: str) -> bool:
    return char in _INVISIBLE_LETTERS_AND_MARKS or any(
        ord(char) in block for block in _VARIATION_SELECTORS
    )
