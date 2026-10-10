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

Inside the brackets, ALL-CAPS acronyms are additionally read letter by
letter ("Integrated Air Defence System (IADS)" -> "... I A D S"), because
Piper otherwise says "IADS" or "NATO" as one word. Which words count is
decided by spell_spans() at the bottom of this module.

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
from typing import Callable, List, Optional, Sequence, Tuple


# Bump when the classification rules or their output change, so that
# cached audio/timings generated under the previous rules are invalidated.
#   v2: ALL-CAPS acronyms inside brackets are read letter by letter
#       ("(IADS)" -> "I A D S"; see spell_spans()).
PARENTHETICAL_CONFIG_VERSION = "v2"


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

    # A stopword whose first letter is also the letter still needed can be
    # EITHER part of the acronym or omitted from it, and only one choice may
    # lead to a full match: "Integrated Architecture AND Display System" is
    # IADS only if "and" is skipped (the A is Architecture), while "Rate Of
    # Climb" is ROC only if "of" is used. So try using the word first and, if
    # that dead-ends, skip it -- plain greedy matching missed the former.
    def matches(letter_i: int, word_i: int) -> bool:

        if letter_i < 0:
            return True

        if word_i < 0:
            return False

        word = window[word_i]

        if not word:
            return matches(letter_i, word_i - 1)

        if word[0].upper() == letters[letter_i] and matches(letter_i - 1, word_i - 1):
            return True

        if word.lower() in INITIALISM_STOPWORDS:
            # Stopword that the acronym omitted -- skip it and retry.
            return matches(letter_i, word_i - 1)

        return False

    return matches(len(letters) - 1, len(window) - 1)


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


# ----------------------------------------------------------------------
# Letter-by-letter reading of bracketed acronyms
#
# Piper says "(IADS)" as "eye-ads" and "(NATO)" as "nay-toe". Inside
# brackets an ALL-CAPS word is nearly always an acronym, so it is spelled
# ("I A D S"). Outside brackets nothing changes: the dictionaries and the
# engine's own pronunciation still apply there.
#
# "Nearly always" is not "always", so a few kinds of word are left alone
# (see spell_spans). The point is to spell acronyms, not to spell every word
# that happens to be shouted.
# ----------------------------------------------------------------------

# An ALL-CAPS word of two or more letters ("IADS", "NATO", "RPM").
CAPS_WORD_RE = re.compile(r"\b[A-Z]{2,}\b")

# Ordinary words that are sometimes written in capitals inside brackets
# ("(NOTE)", "(ON/OFF)", "(SEE TABLE 2)") and must stay words. Editing this
# set changes the spoken output, so it is part of the cache fingerprint (see
# TextPreprocessor._compute_dictionary_fingerprint).
SPEAK_AS_WORD = frozenset({
    "AND", "OR", "NOT", "NO", "YES", "ON", "OFF", "OF", "FOR", "THE", "TO", "WITH", "DO", "IS", "BE",
    "NOTE", "NOTES", "WARNING", "CAUTION", "DANGER",
    "SEE", "REFER", "TASK", "TABLE", "FIGURE", "CHAPTER", "SECTION", "PARA", "PAGE", "STEP", "ITEM",
    "UP", "DOWN", "LEFT", "RIGHT", "HIGH", "LOW", "OPEN", "CLOSED", "LONG", "SHORT",
    # Roman numerals are numbers, not acronyms: "Part (IV)", "Fe(III)". (A bracket
    # that is a list marker is turned into a number before this rule ever runs.)
    "II", "III", "IV", "VI", "VII", "VIII", "IX",
    "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX",
})

# An ALL-CAPS word longer than this that is not a known abbreviation and does
# not abbreviate the words before it is an ordinary word, not an acronym:
# "VACUUM SERVO (BOOSTER)", "MONOCOQUE (UNIBODY)", "(DO NOT OVERFILL)".
SPELL_MAX_LETTERS = 6


def spell_spans(
    body: str,
    kind: ParentheticalKind,
    preceding_text: str,
    expansion_of: Callable[[str], Optional[str]],
    keep_spans: Sequence[Tuple[int, int]] = (),
) -> List[Tuple[int, int]]:
    """
    The (start, end) spans of `body` -- the text between one pair of
    brackets -- whose ALL-CAPS words should be read letter by letter.

      body            the bracket content, brackets and outer spaces removed
      kind            how classify() judged this bracket
      preceding_text  everything before the opening bracket
      expansion_of    word -> the expansion the pipeline's dictionaries
                      give it, or None if they do not know it
      keep_spans      spans of `body` that belong to a multi-word or compound
                      dictionary key ("AIR COND", "TGT / T45"): spelling one
                      of its words would break the key, so none of them is

    Rules, in order, for each ALL-CAPS word of 2+ letters:

      1. INITIALISM bracket  -> spell. It is, by construction, the initials
         of the words just before it ("Time Between Overhaul (TBO)"), so
         it is an acronym whatever it looks like. Nothing below applies.
      2. On SPEAK_AS_WORD, or inside a keep_span -> leave ("NOTE", "ON",
         "OFF", "SEE", the "AIR" of "AIR COND").
      3. A known abbreviation (client dictionary, engineering terms,
         general abbreviations or units):
           - in PROSE         -> leave: the dictionary passes that run
             after this one will say it ("(see FIG 3)" -> "Figure 3",
             "(see the IADS manual)" -> its full name);
           - in a CODE bracket -> nothing will expand it, so spell it if it
             is an initialism of its own expansion (RPM, PSI, IADS) and
             leave it if it is a word or unit abbreviation (FIG, MAX, KG).
      4. Unknown and long (> SPELL_MAX_LETTERS) -> an ordinary word:
         leave it, unless it abbreviates the words before it.
      5. Otherwise            -> spell.
    """

    spans: List[Tuple[int, int]] = []
    preceding_words: Optional[List[str]] = None

    for match in CAPS_WORD_RE.finditer(body):

        word = match.group(0)

        if kind is ParentheticalKind.INITIALISM:
            spans.append(match.span())
            continue

        if word in SPEAK_AS_WORD:
            continue

        if any(match.start() < k_end and match.end() > k_start for k_start, k_end in keep_spans):
            continue

        expansion = expansion_of(word)

        if expansion is not None:

            if kind is ParentheticalKind.PROSE:
                continue

            if not _is_initialism_of(word, WORD_RE.findall(expansion)):
                continue

        elif len(word) > SPELL_MAX_LETTERS:

            if preceding_words is None:
                preceding_words = WORD_RE.findall(preceding_text)

            if not _is_initialism_of(word, preceding_words):
                continue

        spans.append(match.span())

    return spans
