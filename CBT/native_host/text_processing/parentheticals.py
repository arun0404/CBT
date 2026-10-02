"""
Parenthetical normalization for TTS.

Text inside ( ... ) is handled in one of three ways before synthesis:

  1. INITIALISM  -- the content is an acronym for the words immediately
                    preceding it: "Main Gear Box (MGB)".
                    -> brackets dropped, acronym spoken as-is:
                       "Main Gear Box MGB"

  2. CODE        -- the content is a short uppercase / alphanumeric token
                    that is NOT an initialism of what precedes it:
                    "(ATA 62)", "(NVG)".
                    -> brackets dropped, token kept verbatim.

  3. PROSE       -- the content is a normal phrase or sentence:
                    "(after we checked the fuel levels)".
                    -> brackets dropped, content read naturally as words,
                       wrapped in commas so the TTS engine gives it the
                       same parenthetical prosody a human reader would.

In every branch the bracket CHARACTERS are removed rather than verbalized
("open bracket" / "close bracket"), keeping the spoken text clean and
keeping the processed word count tied to real words.

IMPORTANT: this module only classifies and renders text. It does NOT
protect INITIALISM/CODE output from being re-expanded by a later
dictionary pass (e.g. engineering.py may separately define
"RPM" -> "Revolutions Per Minute" for cases where RPM appears bare in
running text). That protection is the caller's responsibility -- see
preprocess.py's use of the placeholder sentinel around this module's
output.

This module is intentionally free of any dependency on the alignment
tracker or the wider pipeline: it exposes pure functions so its rules can
be unit-tested in isolation.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import List


# Bump when the classification rules or their output change, so that
# cached audio/timings generated under the previous rules are invalidated.
PARENTHETICAL_CONFIG_VERSION = "v1"


# Matches a (...) span with no nested parentheses. Content is captured.
# Non-greedy and newline-tolerant so a bracket left unclosed on one line
# cannot swallow the rest of the document.
PARENTHETICAL_RE = re.compile(r"\(([^()\n]{1,200})\)")

# Words that are conventionally dropped when an initialism is formed:
# "Revolution Per Minute" -> RPM, but "Line Replaceable Unit" -> LRU and
# "Federal Aviation Administration" -> FAA. Allowing these to be skipped
# lets "Auxiliary Power Unit (APU)" and "Time Between Overhaul (TBO)"
# both classify correctly.
INITIALISM_STOPWORDS = frozenset({
    "a", "an", "and", "at", "by", "for", "from", "in", "of", "on",
    "or", "per", "the", "to", "with",
})

# How many preceding words we are willing to look back over when trying
# to match an initialism. An acronym longer than this is not credible as
# an abbreviation of its immediate context.
MAX_LOOKBACK_WORDS = 12

# Bounds on what can even be considered an acronym/code token.
MIN_ACRONYM_LENGTH = 2
MAX_ACRONYM_LENGTH = 12

# A token is "code-like" if at least this fraction of its alphabetic
# characters are uppercase. Catches "RPM", "MGB", "NVG", "ATA", and also
# mixed forms like "A/C" or "Pt2" while rejecting ordinary words.
UPPERCASE_RATIO_THRESHOLD = 0.6

# Tokenizers.
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
CONTENT_TOKEN_RE = re.compile(r"\S+")
ALNUM_RE = re.compile(r"[A-Za-z0-9]")


class ParentheticalKind(Enum):
    INITIALISM = "initialism"
    CODE = "code"
    PROSE = "prose"


@dataclass(frozen=True)
class ParentheticalDecision:
    """The classification of one bracketed span and the text that should
    be spoken in its place (brackets already removed)."""

    kind: ParentheticalKind
    content: str
    spoken: str


# ----------------------------------------------------------------------
# Classification helpers
# ----------------------------------------------------------------------

def _letters(token: str) -> str:
    return "".join(ch for ch in token if ch.isalpha())


def _is_code_like(token: str) -> bool:
    """True for short, predominantly-uppercase alphanumeric tokens such as
    RPM, MGB, ATA, A/C, P/N -- i.e. things a reader would say as an
    acronym or code rather than as a word."""

    if not (MIN_ACRONYM_LENGTH <= len(token) <= MAX_ACRONYM_LENGTH):
        return False

    if not ALNUM_RE.search(token):
        return False

    # Reject anything containing characters that would not appear in a
    # code (e.g. commas, sentence punctuation).
    if not re.fullmatch(r"[A-Za-z0-9/.\-]+", token):
        return False

    alpha = _letters(token)

    if not alpha:
        # Pure numeric, e.g. "(62)". Not an acronym; read as prose/number.
        return False

    uppercase = sum(1 for ch in alpha if ch.isupper())

    return (uppercase / len(alpha)) >= UPPERCASE_RATIO_THRESHOLD


def _is_initialism_of(acronym: str, preceding_words: List[str]) -> bool:
    """
    True if `acronym`'s letters are the initials of the tail of
    `preceding_words`, allowing conventional stopwords to be either
    matched or skipped.

    Matching runs right-to-left from the end of the preceding text so
    that "Check the Main Gear Box (MGB)" matches on "Main Gear Box"
    without being confused by earlier words.
    """

    letters = _letters(acronym).upper()

    if len(letters) < MIN_ACRONYM_LENGTH:
        return False

    window = preceding_words[-MAX_LOOKBACK_WORDS:]

    if len(window) < 1:
        return False

    letter_i = len(letters) - 1
    word_i = len(window) - 1

    while letter_i >= 0 and word_i >= 0:

        word = window[word_i]

        if not word:
            word_i -= 1
            continue

        if word[0].upper() == letters[letter_i]:
            letter_i -= 1
            word_i -= 1
            continue

        if word.lower() in INITIALISM_STOPWORDS:
            # Stopword that the acronym omitted -- skip it and retry.
            word_i -= 1
            continue

        return False

    return letter_i < 0


def _as_prose(content: str) -> str:
    """
    Render bracketed prose as a natural spoken aside.

    Commas are inserted around the content so the engine produces the
    slight pause a human reader gives a parenthetical, instead of running
    it straight into the surrounding sentence. Any punctuation already
    terminating the content is preserved rather than doubled.
    """

    body = content.strip()

    if not body:
        return ""

    trailing = "" if body[-1] in ".,;:!?" else ","

    return f", {body}{trailing} "


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def classify(content: str, preceding_text: str) -> ParentheticalDecision:
    """
    Decide how one bracketed span should be spoken.

    `content`        : the text between the brackets, brackets excluded.
    `preceding_text` : everything in the document before the opening
                       bracket. Only its trailing words are consulted.
    """

    body = content.strip()

    if not body:
        return ParentheticalDecision(ParentheticalKind.PROSE, content, "")

    tokens = CONTENT_TOKEN_RE.findall(body)

    # A single, code-like token is a candidate acronym.
    if len(tokens) == 1 and _is_code_like(tokens[0]):

        preceding_words = WORD_RE.findall(preceding_text)

        if _is_initialism_of(tokens[0], preceding_words):
            return ParentheticalDecision(
                kind=ParentheticalKind.INITIALISM,
                content=body,
                spoken=f" {body} ",
            )

        # Uppercase, but not an abbreviation of what came before it --
        # a standalone code such as "(NVG)" or "(ATA)". Still spoken as
        # the token itself, never spelled out as brackets.
        return ParentheticalDecision(
            kind=ParentheticalKind.CODE,
            content=body,
            spoken=f" {body} ",
        )

    # Multi-token content that is *entirely* code-like is still a code,
    # e.g. "(ATA 62)" or "(P/N 4711 AB)".
    if 1 < len(tokens) <= 4 and all(
        _is_code_like(t) or t.isdigit() for t in tokens
    ):
        return ParentheticalDecision(
            kind=ParentheticalKind.CODE,
            content=body,
            spoken=f" {body} ",
        )

    # Everything else is ordinary language and is read verbatim.
    return ParentheticalDecision(
        kind=ParentheticalKind.PROSE,
        content=body,
        spoken=_as_prose(body),
    )
