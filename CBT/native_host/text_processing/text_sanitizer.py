"""
Detection/removal of characters that must never reach the TTS engine:
invisible "zero-width" formatting characters, and emoji.

Why this exists
----------------
Two related bugs, both ultimately Windows console/subprocess ENCODING
issues (not anything wrong with the characters themselves):

    UnicodeEncodeError: 'charmap' codec can't encode character '\\U0001f4c4'
    UnicodeEncodeError: 'charmap' codec can't encode character '\\u200b'

On Windows, a subprocess's stdin/stdout and the parent process's own
console default to the legacy system code page (reported by Python as
the "charmap" codec) unless told otherwise, and that code page cannot
represent most emoji or invisible-formatting code points. That part is
fixed once, at the source, by always encoding as UTF-8 (see piper.py's
subprocess.run(..., encoding="utf-8") and host.py's
sys.stdout.reconfigure(encoding="utf-8")).

This module handles the OTHER half of the requirement: even once the
encoding crash can't happen, we still don't want Piper trying to
pronounce a page emoji or an invisible formatting character out loud.
So this module strips them from the text that's actually sent to
Piper, while the ORIGINAL text (what the browser highlights) is left
completely untouched by callers — see
TextPreprocessor.strip_tts_only_characters() in preprocess.py, which
applies this against the AlignmentTracker's working copy only.

`is_pure_emoji_word()` is the other consumer: it lets timing.py flag
which per-word timing entries are "silent" (all emoji/invisible, so
Piper never spoke them and their natural timing slot is vanishingly
short), so the frontend can hold the highlight on them for a fixed,
readable dwell instead of flickering past — see highlighter.js.
"""

import re

# --------------------------------------------------------------------
# Invisible / zero-width formatting characters
#
# These are legitimate Unicode characters (often introduced silently
# by copy/pasting from a word processor, PDF, or web page) that render
# as nothing but are NOT whitespace, so they survive naive trimming
# and can end up mid-word in the text handed to Piper.
# --------------------------------------------------------------------
_INVISIBLE_CHARS = (
    "\u200B"  # zero width space
    "\u200C"  # zero width non-joiner
    "\u200D"  # zero width joiner (also used to glue compound emoji
              # together, e.g. family/profession sequences — safe to
              # strip unconditionally since every emoji it could be
              # joining is itself being stripped too)
    "\u200E"  # left-to-right mark
    "\u200F"  # right-to-left mark
    "\u2060"  # word joiner
    "\uFEFF"  # zero width no-break space / byte order mark
    "\u180E"  # Mongolian vowel separator
    "\u00AD"  # soft hyphen
)

# --------------------------------------------------------------------
# Emoji ranges safe to strip UNCONDITIONALLY before Piper synthesis.
#
# Deliberately excludes:
#   - U+2190-U+22FF (Arrows / Mathematical Operators) — already
#     expanded to spoken words by SYMBOLS in symbols.py (->, <=, etc).
#   - U+2300-U+23FF (Misc Technical) — contains the engineering
#     diameter sign (U+2300, "⌀") and similar drafting notation that
#     must remain speakable in a technical manual, not be treated as
#     decorative.
# If a specific symbol in these excluded ranges should also be spoken
# (e.g. "⚠" as "Warning"), add it to SYMBOLS in symbols.py instead of
# widening this module — that keeps "things Piper should say" and
# "things Piper should never see" as two clearly separate lists.
# --------------------------------------------------------------------
_EMOJI_RANGES = (
    (0x1F1E6, 0x1F1FF),  # Regional indicator symbols (flag letters)
    (0x1F300, 0x1F5FF),  # Misc symbols & pictographs
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F680, 0x1F6FF),  # Transport & map symbols
    (0x1F700, 0x1F77F),  # Alchemical symbols
    (0x1F780, 0x1F7FF),  # Geometric shapes extended
    (0x1F800, 0x1F8FF),  # Supplemental arrows-C
    (0x1F900, 0x1F9FF),  # Supplemental symbols & pictographs
    (0x1FA00, 0x1FA6F),  # Chess symbols
    (0x1FA70, 0x1FAFF),  # Symbols & pictographs extended-A
    (0x2600, 0x26FF),    # Misc symbols
    (0x2700, 0x27BF),    # Dingbats
    (0x2B00, 0x2BFF),    # Misc symbols & arrows (stars, etc.)
    (0xFE0F, 0xFE0F),    # Variation selector-16 (emoji presentation)
)


def _char_class(chars: str = "", ranges=()) -> str:
    """Builds a `[...]` regex character class from literal characters
    plus (lo, hi) codepoint ranges."""

    body = chars + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in ranges)
    return f"[{body}]"


INVISIBLE_CHAR_PATTERN = re.compile(_char_class(chars=_INVISIBLE_CHARS))
EMOJI_PATTERN = re.compile(_char_class(ranges=_EMOJI_RANGES))

# What actually gets removed before a piece of text is sent to Piper.
# The trailing "+" merges consecutive matches (e.g. an emoji plus its
# variation selector, or a multi-codepoint flag sequence) into a
# single replacement instead of one per codepoint.
TTS_STRIP_PATTERN = re.compile(
    _char_class(chars=_INVISIBLE_CHARS, ranges=_EMOJI_RANGES) + "+"
)


def strip_invisible_and_emoji(text: str) -> str:
    """
    Plain-string convenience wrapper (no alignment tracking): removes
    every invisible/zero-width character and emoji from `text`.

    The live pipeline (TextPreprocessor.process_with_alignment) does
    NOT call this directly — it applies TTS_STRIP_PATTERN against an
    AlignmentTracker instead, so word-origin provenance survives the
    removal. This function is for any caller that only needs plain
    text back (e.g. a debug/preview endpoint, or a unit test).
    """

    if not text:
        return text

    return TTS_STRIP_PATTERN.sub("", text)


# --------------------------------------------------------------------
# Structural marks nobody speaks: dashes and quotation marks.
#
# Measured against the real Piper voices: a quote (straight or curly), an
# NBSP or a zero-width space adds no audio, and a dash at the edge of the
# text or glued to a word is ignored. A STANDALONE en/em dash between words
# adds a pause, which preprocess.py replaces with a comma, so it never
# reaches Piper as a dash. None of these ever produces a spoken word, which
# is exactly what "silent" means to the timing code and to highlighter.js.
#
# Deliberately NOT here: "°", arrows, "©", "®", "™" and friends. They are
# in SYMBOLS and ARE spoken ("degrees", "leads to", "copyright").
# --------------------------------------------------------------------
_DASH_CHARS = "-‐‑‒–—―−"
_QUOTE_CHARS = "\"'‘’‚‛“”„‟«»‹›"

NON_SPOKEN_ONLY_PATTERN = re.compile(
    "(?:"
    + "|".join(re.escape(c) for c in _INVISIBLE_CHARS + _DASH_CHARS + _QUOTE_CHARS)
    + "|" + _char_class(ranges=_EMOJI_RANGES)
    + r"|\s)+"
)


def is_non_spoken_word(word: str) -> bool:
    """
    True if `word` -- one \\S+ token of the ORIGINAL text -- contains
    nothing a TTS engine would say: only dashes, quotation marks,
    invisible/zero-width characters, emoji and whitespace ("–", '"', "“”",
    "📄", NBSP, a zero-width space ...).

    A superset of is_pure_emoji_word(). Such a token keeps its place in the
    word list (so the browser's word indices stay in step with the timings)
    but is flagged silent: it is never a highlight or seek target.

    A token that MIXES these with real text ("📄Document", '"Replace') is
    NOT non-spoken; it has a spoken part and gets a normal timing window.
    """

    if not word:
        return False

    return NON_SPOKEN_ONLY_PATTERN.fullmatch(word) is not None


def is_pure_emoji_word(word: str) -> bool:
    """
    True if `word` — one \\S+ token from the ORIGINAL/authored text,
    e.g. one entry of alignment.original_words — is made up entirely
    of emoji and/or invisible characters, i.e. it carries no text a
    TTS engine would ever actually speak.

    A token that MIXES emoji with real text (e.g. "📄Document") is
    NOT considered "pure" — it still has genuine spoken content and
    gets a normal, real timing window from the audio, so it shouldn't
    be treated as a silent/zero-duration word.
    """

    if not word:
        return False

    remainder = TTS_STRIP_PATTERN.sub("", word)

    return remainder.strip() == ""
