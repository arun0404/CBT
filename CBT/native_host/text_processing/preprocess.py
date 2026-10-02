import hashlib
import json
import logging
import os
import re
from itertools import groupby
from typing import List

# NOTE: num2words is deliberately NOT imported here any more. The only
# remaining number-to-word conversion in this module is the fixed ten-way
# digit mapping in _DIGIT_WORDS below. num2words is still a real runtime
# dependency of the project — aligner/forced_aligner.py uses it to spell
# numbers for the MMS_FA tokenizer, which has no digit tokens — so it
# stays in requirements.txt.
from .engineering import ENGINEERING_TERMS
from .abbreviations import ABBREVIATIONS
from .symbols import SYMBOLS
from .units import UNITS
from .alignment import AlignmentTracker, AlignmentResult, Verbatim
from .text_sanitizer import TTS_STRIP_PATTERN
from .parentheticals import (
    PARENTHETICAL_RE,
    PARENTHETICAL_CONFIG_VERSION,
    ParentheticalKind,
    classify as classify_parenthetical,
)

logger = logging.getLogger(__name__)

# Sentinel used to shield an already-decided acronym/code (e.g. the "RPM"
# produced from "(RPM)") from being re-expanded by a LATER dictionary pass
# that happens to also define "RPM" -> "Revolutions Per Minute" (see
# engineering.py). \x02 ("start of text") never appears in authored
# manual text and is not matched by \w, a unit-suffix pattern, or any
# symbol, so it can't collide with anything downstream.
#
# NOT \x1E: despite being a non-printable "record separator", \x1E (like
# \x1C/\x1D/\x1F) is classified as whitespace by Python's str.isspace()
# and therefore by \s in every regex here -- a bare "\s*" in a later pass
# (e.g. replace_units' unit-suffix pattern) can match straight through
# it, silently deleting the placeholder's delimiter and corrupting the
# protected text. \x02 is not whitespace, so it can't be skipped that
# way.
#
# The placeholder is restored to real text after all dictionary/unit/
# symbol passes, right before word counts are finalized.
_PLACEHOLDER_RE = re.compile(r"\x02PH(\d+)\x02")

# A separate placeholder tag for protected reference/task codes (see
# protect_reference_codes below) -- kept independent of _PLACEHOLDER_RE
# so the two protection mechanisms can never collide or be restored out
# of order.
_REFERENCE_CODE_PLACEHOLDER_RE = re.compile(r"\x02RC(\d+)\x02")

# Sentinel inserted by the FRONTEND (currentChapterText() in app.js)
# immediately after each heading (<h1>-<h4>) element's own text, before
# that string is POSTed to /speak -- marks where Piper should take a
# natural pause before the following content starts.
#
# By the time text reaches this module, all HTML/DOM structure is
# already gone (app.js builds the request body from #content.innerText),
# so nothing here can tell "this line was a heading" from "this line was
# a paragraph" on its own -- this marker is what carries that one bit of
# structure across that boundary.
#
# \x1f ("unit separator") is deliberately WHITESPACE per Python's
# str.isspace()/regex \s -- same C0 "separator" bidi-class behavior this
# file already documents for \x1e above, just used in the OPPOSITE
# direction: those placeholders needed a NON-whitespace sentinel so a
# unit-suffix pattern's bare "\s*" couldn't match through them; this one
# needs to BE whitespace, so inserting it can never create a new \S+
# token. ORIGINAL_WORD_RE (alignment.py) and PROCESSED_WORD_RE only ever
# see it as part of an existing whitespace run -- original_words/
# processed_words counts stay exactly what they'd be without it, so
# nothing downstream (word grouping, forced-alignment timing, the DOM
# index highlighter.js highlights) shifts by even one position. It never
# appears in the DOM itself, only in the string extracted from #content.
_HEADING_PAUSE_MARKER = "\x1f"
_HEADING_PAUSE_MARKER_RE = re.compile(_HEADING_PAUSE_MARKER)

# A multi-segment hyphenated reference/task code -- e.g. an ATA task
# number "79-24-00-870-7A" -- at least 3 numeric segments, with any
# number of short trailing letter suffixes ("...-920A-A"). Deliberately
# requires 3+ segments (not 2) so a genuine two-value unit range like
# "5mm-10mm" is never mistaken for one: a range only ever has ONE hyphen
# between two already unit-suffixed numbers, never a chain of bare
# digit groups.
#
# The leading "(?:(?<=\w)-)?" optionally swallows a single hyphen that
# sits BETWEEN the code and an immediately-preceding word character --
# i.e. when the code was written glued onto an alphabetic prefix such as
# "ALHMKMTM-79-24-00-00-920A-A" or "ALHMKMTM-SYSTEM-79-24-00-00-920A-A".
# Without this the hyphen is left dangling once the numeric part is
# replaced by its placeholder, and later fuses onto whatever the
# placeholder restores to ("SYSTEM" + "seventy-nine" -> one un-alignable
# "SYSTEMseventy-nine" token, wrecking forced-alignment timing). The
# lookbehind keeps it from ever eating the "-" of a real range.
REFERENCE_CODE_RE = re.compile(r"(?:(?<=\w)-)?\b\d+(?:-\d+){2,}(?:-?[A-Za-z]{1,3})*\b")

# -------------------------------------------------------
# Roman-numeral list markers ("i.", "ii)", "iii.", ...) -> spoken cardinal
#
# The manual authors indent lists this way throughout (see e.g.
# "i. Main Gear Box (MGB)<br>ii. Intermediate Gear Box (IGB)..." and
# "i) Intermediate Gear Box (IGB) Assembly<br>ii) Tail Gear Box..." in
# data2.json) -- without this, Piper reads the marker letter-by-letter
# ("i dot" / "i eye dot"), not as an enumeration.
#
# Deliberately case-SENSITIVE (lowercase only, no re.IGNORECASE): "I."
# is the pronoun "I" ending a sentence ("... as discussed. I. ...") far
# more often than it is a list marker in real prose, and that
# capitalized form is never how this manual writes its lists. Restricting
# to lowercase is what keeps that sentence-final "I." from ever being
# mistaken for item one of a list.
#
# Scoped to the START of a line (re.MULTILINE "^", i.e. right after a
# "<br>"-turned-newline or the very start of the text) specifically so an
# ordinary word or abbreviation elsewhere in a sentence is never touched
# -- "vs. i." mid-sentence, or "i.e." (excluded separately below, since
# that already starts a fresh clause after its own preceding period and
# so CAN land at a line start). The trailing (?=\s|$) requires the
# marker to be followed by whitespace or end-of-string, which is what
# actually excludes "i.e." -- there is no space between "i." and "e".
#
# Extended a little past the "i-x" the manual actually uses (up to "xx")
# since a longer procedure list is plausible and the marginal false-
# positive risk of also claiming, say, a line that happens to start with
# a bare "xv." is the same trade-off already accepted for "i"/"v"/"x".
ROMAN_NUMERAL_LIST_WORDS = {
    "i": "One",
    "ii": "Two",
    "iii": "Three",
    "iv": "Four",
    "v": "Five",
    "vi": "Six",
    "vii": "Seven",
    "viii": "Eight",
    "ix": "Nine",
    "x": "Ten",
    "xi": "Eleven",
    "xii": "Twelve",
    "xiii": "Thirteen",
    "xiv": "Fourteen",
    "xv": "Fifteen",
    "xvi": "Sixteen",
    "xvii": "Seventeen",
    "xviii": "Eighteen",
    "xix": "Nineteen",
    "xx": "Twenty",
}

# Longest numeral first within the alternation -- "iii" must get the
# chance to match in full before "ii"/"i" do (Python's alternation tries
# left-to-right and only backtracks into a later branch if the REST of
# the pattern then fails, so this isn't strictly load-bearing given the
# trailing [.)] anchor below, but it removes any dependence on that
# backtracking behavior at all).
_ROMAN_LIST_ALTERNATION = "|".join(
    sorted(ROMAN_NUMERAL_LIST_WORDS.keys(), key=len, reverse=True)
)

ROMAN_LIST_MARKER_RE = re.compile(
    r"(?m)^[ \t]*(" + _ROMAN_LIST_ALTERNATION + r")[.)](?=\s|$)"
)

# -------------------------------------------------------
# "ABS" (Anti-Lock Braking System) -> spelled out letter-by-letter
#
# At 3 letters, "ABS" is below _INITIALISM_RE's 4-letter threshold (see
# _is_unpronounceable's own comment: NASA/NATO/STOP/NORTH-length runs are
# left alone because a real vowel breaks them before the "unpronounceable
# consonant cluster" heuristic ever fires) and isn't in any of the
# ABBREVIATIONS/ENGINEERING_TERMS/client dictionaries either, so with no
# rule at all it reaches Piper as the bare 3 letters "ABS" -- which
# espeak-ng's phonemizer reads as the ordinary English word "abs" (short
# vowel, one syllable, as in abdominal muscles), not as an initialism.
#
# Deliberately case-SENSITIVE (no re.IGNORECASE) with \b on both sides:
# this only ever matches the standalone, all-caps token "ABS", never
# "abs"/"Abs" inside ordinary prose, and never a partial match inside a
# longer word ("absorb", "absence") -- \b already blocks that on its own
# for a 3-character span, but staying case-sensitive too costs nothing
# and removes any dependency on that alone.
#
# "A B S" (no periods) rather than "A. B. S.": Piper is invoked with
# --sentence_silence (see config.py/piper.py, added for the heading-pause
# feature) specifically to make a period render as an audible ~500ms
# gap. Three periods packed into one 3-letter acronym would turn "ABS"
# into three separate half-second silences between letters, which reads
# as broken, not as a spoken initialism -- the opposite of what this
# rule exists to fix.
_ABS_ACRONYM_RE = re.compile(r"\bABS\b")

# A STANDALONE dash -- one with whitespace on BOTH sides -- is prose
# punctuation (an em/en-dash used as a sentence break), not part of a
# word. Piper's phonemizer reads such a bare "-" aloud as the word
# "dash", so "Mounting Bracket - Driveshaft Support Bracket" was spoken
# as "Mounting Bracket DASH Driveshaft Support Bracket".
#
# symbols.py deliberately leaves "-" unmapped (its '"-": " minus "' entry
# is commented out) and that is correct: a blanket rule would wreck every
# compound word and hyphenated number -- "Anti-Lock" -> "Anti minus
# Lock", "seventy-nine" -> "seventy minus nine". The requirement of
# BOUNDING whitespace on both sides is what makes this pass safe: an
# intra-word hyphen has none, so "Anti-Lock", "five-speed", the unit
# range "5mm-10mm", and a reference code's own hyphens are all
# untouchable by it.
#
# En-dash and em-dash are included since authored content (and anything
# pasted from a word processor) uses them for exactly the same purpose.
# The replacement is ", " -- a real pause, which is what a dash means
# here -- rather than a deleted character, so the two clauses don't run
# together into one breathless phrase.
_STANDALONE_DASH_RE = re.compile(r"\s+[-–—]+\s+")

# A run of two or more unspaced alphanumeric words joined by forward
# slashes -- "task/document", "inspection/check", "L/W/H". Each slash is
# spoken as "or" ("task or document", "L or W or H"). The \b anchors and
# the "no whitespace anywhere in the run" shape keep it off file paths,
# URLs ("https://" -- the "//" has no alnum between the slashes),
# doubled/leading/trailing slashes, and anything with a space already
# around a slash. A run that is itself a unit ("km/h", "m/s") is caught
# here too and expanded to the spoken unit name instead of "or" -- see
# normalize_slashes().
_SLASH_COMPOUND_RE = re.compile(r"\b[A-Za-z0-9]+(?:/[A-Za-z0-9]+)+\b")

# Dimension abbreviations. Only ever expanded inside an explicit
# dimension pattern -- NOT as standalone dictionary keys (see the note in
# engineering.py) -- so a lone "L"/"W"/"H" in running text, or the "h" of
# "km/h", is never touched.
_DIMENSION_WORDS = {"L": "Length", "W": "Width", "H": "Height"}

# "L x W x H", "L X W", "H×W", "LxW" -- two or three dimension letters
# multiplied together. Case-sensitive on the letters (dimension labels
# are conventionally upper-case); the separator may be x / X / the
# multiplication sign.
_DIMENSION_TRIPLE_RE = re.compile(
    r"\b([LWH])\s*[xX×]\s*([LWH])(?:\s*[xX×]\s*([LWH]))?\b"
)

# "H:", "W :", "L:" immediately labelling a numeric value ("H: 25mm").
# The digit lookahead keeps it off prose like "Section L: overview".
_DIMENSION_LABEL_RE = re.compile(r"\b([LWH])\s*:(?=\s*\d)")

# A run of 4+ consecutive uppercase letters that is a candidate
# initialism or code prefix (e.g. "ALHMKMTM"). The \b anchors ensure
# we never split a token that has trailing digits or lowercase characters
# attached (those are handled by other passes).
_INITIALISM_RE = re.compile(r"\b([A-Z]{4,})\b")

# Uppercase vowels, INCLUDING Y. Y earns its place here: in an all-caps
# string it is overwhelmingly a syllable nucleus ("SYSTEM", "RHYTHM",
# "CRYPT", "GLYPH", "NYMPH") rather than the glide it can be in lower
# case, and counting it as a consonant made _is_unpronounceable fire on
# those ordinary words and spell them out letter-by-letter ("S Y S T E M")
# -- the spurious pronunciation this module must not produce. The genuine
# targets (ALHMKMTM, HTML, HTTPS, HMRC) still have a 4+ run of true
# consonants and are unaffected; only a contrived Y-riddled code like
# "GYLY" slips through now, which is an acceptable trade.
_VOWELS = frozenset("AEIOUY")


def _is_unpronounceable(word: str) -> bool:
    """
    True when `word` contains a run of 4+ consecutive consonants, where
    A/E/I/O/U/Y all count as vowels that break the run (see _VOWELS).

    That threshold reliably separates letter-strings that no English
    phoneme rule covers (ALHMKMTM, HTML, HTTPS, HMRC) from real words and
    from abbreviations Piper pronounces fine (NASA, NATO, STOP, NORTH --
    each has a vowel breaking any cluster before it reaches 4). Counting
    Y as a vowel is what keeps ordinary words such as "SYSTEM" ("SYST"
    would otherwise read as a 4-consonant run) or "RHYTHM" from being
    spelled out letter-by-letter -- the spurious pronunciation this pass
    must never produce.

    All dictionary-known terms (RPM → "Revolutions Per Minute", etc.)
    are expanded by an earlier pass, so they never reach this function.
    """
    run = 0
    for ch in word:
        if ch not in _VOWELS:
            run += 1
            if run >= 4:
                return True
        else:
            run = 0
    return False


# -------------------------------------------------------
# Reference/task code -> natural speech
#
# A code like "79-24-00-870-7A" must NOT reach Piper with its hyphens
# intact -- Piper's phonemizer reads a bare "-" as the word "dash" and
# mangles a doubled "00" into an unintelligible sound rather than two
# distinct zeros. Instead the whole code is spelled out digit-by-digit as
# ONE continuous run of plain words separated by single spaces, with no
# internal punctuation of any kind:
#
#     "79-24-00-870-7A" -> "seven nine two four double zero eight seven zero seven A"
#
# The segment boundaries used to become ", " (a comma per hyphen, for an
# audible pause between groups). That was removed: with
# --sentence_silence in force (see config.py/piper.py) every one of those
# commas asks the phonemizer for a clause break MID-CODE, and a break
# landing immediately after a digit word clips its trailing vowel into an
# artefact instead of a clean word. A code is read as one uninterrupted
# digit run for that reason -- single spaces only, so every digit word is
# a complete, independently-phonemized token.
#
# Per-segment reading rule: DIGIT-BY-DIGIT, with a run of the same digit
# collapsed into a counted phrase (see _REPEAT_PREFIXES below).
#
#     "79"  -> "seven nine"        (not "seventy-nine")
#     "24"  -> "two four"          (not "twenty-four")
#     "00"  -> "double zero"       (not "zero zero" -- see below)
#     "000" -> "triple zero"
#     "870" -> "eight seven zero"  (not "eight-seventy")
#     "7A"  -> "seven A"
#
# These identifiers are not quantities -- nothing is "eight hundred and
# seventy" of anything -- they are opaque strings where each digit is a
# separate field of meaning (system / sub-system / unit / task). Reading
# them as cardinal numbers, which is what this module used to do, invites
# a listener to mishear an aggregated value ("eight-seventy") and lose
# the actual digit sequence they need in order to look the workcard up.
# Digit-by-digit is also how these are read aloud in practice.
#
# Digits within one segment are joined by SPACES, not hyphens. Both are
# safe for alignment -- every character of the spoken form carries the
# same origin (see protect_reference_codes' Verbatim below), so the
# one-original-word-expands-to-many-processed-words case is exactly what
# alignment.py's _build_groups already handles, and the processed word
# count is free to differ. Spaces win on PRONUNCIATION: PROCESSED_WORD_RE
# (\b[\w'-]+\b) treats a hyphen as word-internal, so "seven-nine" would
# be a single token, and unlike a genuine English compound such as
# "seventy-nine" it is not a spelling espeak-ng is guaranteed to voice
# cleanly. Separate words are unambiguous.
# -------------------------------------------------------


# How a 0 inside a reference/task code is spoken. Plain "zero" -- the
# unambiguous, professional reading for a technical identifier. ("oh" was
# tried and reverted: it is shorter but reads as informal, and being a
# repeated token itself it never addressed the under-articulation it was
# meant to fix.)
#
# Isolated as a named constant because it is the one knob worth turning
# while tuning how codes sound.
#
# Scope: _DIGIT_WORDS feeds _speak_digit_run, which is reached ONLY via
# _speak_reference_segment / _speak_reference_code. Ordinary prose
# numbers never pass through here (they are left as literal digits for
# espeak to voice), so this affects reference codes and nothing else.
_ZERO_WORD = "zero"

# Explicit digit -> spoken word. Pinned here rather than derived from
# num2words so that a num2words version or default-locale change can
# never silently alter how a workcard number is pronounced (num2words is
# locale-aware; these ten words must not be).
_DIGIT_WORDS = {
    "0": _ZERO_WORD,
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}

# A run of the SAME digit is collapsed into a counted phrase rather than
# the word repeated back-to-back: "00" -> "double zero", "000" ->
# "triple zero".
#
# This is the one transformation that actually addresses the reported
# symptom. Attention-based neural TTS (Piper/VITS) is known to
# under-articulate or skip a token repeated immediately after itself, and
# "zero zero" is exactly that shape. "double zero" removes the repetition
# entirely -- no two adjacent words are identical any more -- so there is
# nothing for the decoder to collapse. It is also how a technician reads
# the code aloud, so it costs nothing in clarity.
#
# Crucially this needs NO punctuation, so it cannot interact with
# SENTENCE_SILENCE_SECONDS (config.py): a period per digit would have
# inserted ~0.5s of silence between every digit, turning an 11-digit code
# into several seconds of dead air.
_REPEAT_PREFIXES = {2: "double", 3: "triple"}


def _chunk_repeat_run(length: int) -> List[int]:
    """
    Split a run of `length` identical digits into group sizes that all
    have a counted prefix -- i.e. only 2s and 3s, never a bare 1 unless
    the run really is a single digit.

        1 -> [1]        4 -> [2, 2]      6 -> [2, 2, 2]
        2 -> [2]        5 -> [2, 3]      7 -> [2, 2, 3]
        3 -> [3]

    Avoiding a trailing 1 is the point: decomposing 4 as [3, 1] would
    emit "triple zero zero", reintroducing the adjacent-identical-word
    pattern this whole scheme exists to remove.
    """

    if length <= 1:
        return [length]

    parts: List[int] = []
    remaining = length

    while remaining > 3:
        parts.append(2)
        remaining -= 2

    parts.append(remaining)

    return parts


def _speak_digit_run(digits: str) -> str:
    """
    Digit-by-digit, with runs of the same digit counted:

        "870" -> "eight seven zero"
        "00"  -> "double zero"
        "000" -> "triple zero"
    """

    spoken: List[str] = []

    for digit, group in groupby(digits):

        word = _DIGIT_WORDS[digit]

        for size in _chunk_repeat_run(len(list(group))):

            prefix = _REPEAT_PREFIXES.get(size)

            spoken.append(word if prefix is None else f"{prefix} {word}")

    return " ".join(spoken)


_REFERENCE_SEGMENT_RE = re.compile(r"^(\d*)([A-Za-z]*)$")


def _speak_reference_segment(segment: str) -> str:

    match = _REFERENCE_SEGMENT_RE.match(segment)
    digits, letters = match.groups() if match else (segment, "")

    parts = []

    if digits:
        parts.append(_speak_digit_run(digits))

    if letters:
        # UPPERCASE, deliberately. A standalone lowercase "a" is the
        # English article, and espeak-ng voices it as a reduced schwa
        # ("uh") rather than the letter name "ay" -- so "7A" came out as
        # "seven uh", a mumbled, incomplete-sounding tail on every
        # workcard number. An isolated CAPITAL letter is read as its
        # name instead, which is the same thing expand_initialisms()
        # below relies on when it spells out an unpronounceable run
        # ("ALHMKMTM" -> "A L H M K M T M"). Keeping the case consistent
        # between the two letter-spelling paths is what makes them sound
        # alike.
        parts.append(" ".join(letters.upper()))

    return " ".join(parts) if parts else segment


def _speak_reference_code(code: str) -> str:
    """
    Converts a matched reference/task code (e.g. "79-24-00-870-7A") into
    its spoken form -- "seven nine two four double zero eight seven zero
    seven A" -- see the module-level comment above for the per-segment
    reading rule and for why there is no internal punctuation.

    Joined with a single SPACE, deliberately: a comma per segment
    boundary made the phonemizer break mid-code and clip the word before
    it. Losing the pauses costs some of the code's visible field
    structure, but a clean, fully-pronounced digit run is worth more to a
    listener writing the number down than group pauses they can't act on
    anyway.
    """

    return " ".join(_speak_reference_segment(seg) for seg in code.split("-"))


class TextPreprocessor:

    def __init__(self):

        self.client_dictionary = self.load_client_dictionary()

        # A short, stable fingerprint of every dictionary this pipeline
        # depends on. Changing ANY abbreviation/unit/symbol entry
        # changes this value, which is used as part of the alignment
        # cache key so stale cached timings can never be served after
        # a dictionary edit — see cache.py.
        self.dictionary_fingerprint = self._compute_dictionary_fingerprint()

    # -------------------------------------------------------
    # Load Client Dictionary
    # -------------------------------------------------------

    def load_client_dictionary(self):

        path = os.path.join(
            os.path.dirname(__file__),
            "client_dictionary.json"
        )

        if not os.path.exists(path):
            return {}

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # -------------------------------------------------------
    # Dictionary Fingerprint
    # -------------------------------------------------------

    def _compute_dictionary_fingerprint(self) -> str:

        payload = json.dumps(
            {
                "client_dictionary": self.client_dictionary,
                "engineering_terms": ENGINEERING_TERMS,
                "abbreviations": ABBREVIATIONS,
                "units": UNITS,
                "symbols": SYMBOLS,
                "parenthetical_rules": PARENTHETICAL_CONFIG_VERSION,
            },
            sort_keys=True
        )

        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # -------------------------------------------------------
    # Heading pause markers -> real sentence-ending punctuation
    #
    # There is no SSML/break-tag support anywhere in this pipeline --
    # Piper is fed plain text -- so punctuation is the only lever
    # available for pacing. Runs first: the "does this heading already
    # end in punctuation" check below needs to see the text exactly as
    # authored, before any later pass rewrites what precedes the marker.
    # -------------------------------------------------------

    def insert_heading_pauses(self, tracker: AlignmentTracker) -> None:

        if _HEADING_PAUSE_MARKER not in tracker.text:
            return

        def _repl(match):

            preceding = tracker.text[:match.start()].rstrip()

            # A heading that already ends in its own punctuation doesn't
            # need a second period stacked on top of it -- just make sure
            # the marker still leaves a real space behind so it can never
            # fuse the heading's last word onto whatever follows.
            if preceding and preceding[-1] in ".!?:":
                return " "

            return ". "

        tracker.apply(_HEADING_PAUSE_MARKER_RE, _repl)

    # -------------------------------------------------------
    # Roman-numeral list markers -> spoken cardinal ("i." -> "One.")
    #
    # See the ROMAN_LIST_MARKER_RE module comment above for why this is
    # case-sensitive/line-start-scoped. Runs early, alongside the other
    # structural passes, before any dictionary pass gets a chance to
    # touch the surrounding text.
    #
    # apply() (not apply_segments()) is correct here: the marker plus its
    # "."/")" is exactly ONE original \S+ token, so there is nothing
    # multi-word to preserve separately -- the same category of
    # substitution as a plain dictionary expansion (e.g. "RPM" ->
    # "Revolutions Per Minute"), just driven by a regex instead of a
    # dict lookup.
    # -------------------------------------------------------

    def expand_roman_numeral_list_markers(self, tracker: AlignmentTracker) -> None:

        def _repl(match):
            return ROMAN_NUMERAL_LIST_WORDS[match.group(1)] + "."

        tracker.apply(ROMAN_LIST_MARKER_RE, _repl)

    # -------------------------------------------------------
    # Standalone prose dash -> comma pause
    #
    # See the _STANDALONE_DASH_RE comment above for why this is scoped to
    # whitespace-bounded dashes only, and why symbols.py deliberately
    # can't handle it with a blanket rule.
    #
    # apply() rather than apply_segments(): the dash (with its
    # surrounding whitespace) is its own \S+ token in the original text,
    # so there is no interior content whose per-character origin needs
    # preserving -- the union origin apply() assigns is already right.
    # -------------------------------------------------------

    def normalize_standalone_dashes(self, tracker: AlignmentTracker) -> None:

        tracker.apply(_STANDALONE_DASH_RE, lambda m: ", ")

    # -------------------------------------------------------
    # Strip TTS-only characters (emoji / invisible formatting)
    #
    # Runs FIRST, before every other pass, for two reasons:
    #
    #   1. It's a pure sanitization step with no semantic dependency on
    #      anything downstream, so there's no reason for later passes
    #      (parentheticals, dictionary lookups, unit/symbol expansion)
    #      to ever have to deal with stray emoji or zero-width
    #      characters sitting inside a match.
    #
    #   2. It operates on tracker.text (the WORKING/spoken copy), never
    #      on tracker.original_text — original_words is captured once,
    #      in AlignmentTracker.__init__, from the raw input text before
    #      this (or any) pass runs. So emoji stay fully visible in the
    #      browser's DOM (original_words / highlighter.js) while being
    #      completely absent from what Piper actually receives.
    #
    # See text_sanitizer.py for exactly which characters this removes
    # and why.
    #
    # A stripped run is replaced by a SINGLE SPACE, not by nothing,
    # UNLESS it sits between two word characters. Rationale:
    #
    #   - A leading/standalone emoji ("📄 ALHMKMTM-..") or a symbol
    #     between two words must leave a whitespace gap behind so the
    #     AlignmentTracker keeps a real character slot (carrying that
    #     emoji's own original-word origin) at that position, and the
    #     words on either side never fuse into one token. Emitting ""
    #     there dropped every trace of the non-BMP character from the
    #     origin map, which is what let a multi-codepoint emoji shift
    #     the origin/grouping of the words that followed it.
    #
    #   - An INVISIBLE character wedged inside a single word
    #     ("super​script", from a bad copy/paste) must still be
    #     deleted outright so the word rejoins — inserting a space there
    #     would split one spoken word into two.
    #
    # Redundant whitespace this introduces is collapsed by cleanup().
    # -------------------------------------------------------

    def strip_tts_only_characters(self, tracker: AlignmentTracker) -> None:

        if not TTS_STRIP_PATTERN.search(tracker.text):
            return

        removed_count = len(TTS_STRIP_PATTERN.findall(tracker.text))

        logger.debug(
            "Stripping %d emoji/invisible-character run(s) before "
            "TTS synthesis (original text is unaffected).",
            removed_count
        )

        def _repl(match):
            text = match.string
            before = text[match.start() - 1] if match.start() > 0 else ""
            after = text[match.end()] if match.end() < len(text) else ""
            if before.isalnum() and after.isalnum():
                return ""
            return " "

        tracker.apply(TTS_STRIP_PATTERN, _repl)

    # -------------------------------------------------------
    # Parentheticals
    #
    # Runs before any dictionary pass, for two reasons:
    #
    #   1. The acronym test needs to look at the words that PRECEDE the
    #      bracket exactly as the author wrote them. Once "Main Gear Box"
    #      has been rewritten by a dictionary, the initials no longer
    #      line up with "(MGB)".
    #
    #   2. Once we've decided "(RPM)" should be spoken as the bare
    #      acronym "RPM", that "RPM" must NOT be visible to the later
    #      ENGINEERING_TERMS pass -- which separately maps bare
    #      "RPM" -> "Revolutions Per Minute" for the (different) case of
    #      RPM appearing un-bracketed in running text. Without
    #      protection, the dictionary pass matches its own output a
    #      second time and the phrase gets spoken twice:
    #      "Revolution Per Minute Revolutions Per Minute".
    #
    # INITIALISM / CODE output is therefore swapped for an inert
    # placeholder token here, and only restored to real text after every
    # dictionary/unit/symbol pass has already run. PROSE content is
    # substituted directly (no placeholder) since it's ordinary language
    # and SHOULD still have abbreviations inside it expanded normally,
    # e.g. "(after checking the ECU)" -> "(after checking the
    # Engine Control Unit)".
    # -------------------------------------------------------

    def replace_parentheticals(self, tracker: AlignmentTracker) -> List[Verbatim]:

        # NOTE: this uses apply_segments(), not apply(), specifically so
        # a multi-word parenthetical -- "(Refer to Task 79-24-00-870-7A)",
        # "(after we checked the fuel levels)" -- keeps each interior
        # word's OWN per-character origin instead of having all of them
        # smeared into one undifferentiated group. Only the bracket
        # characters and any inserted punctuation are genuinely NEW
        # content; the words between them are carried through verbatim
        # and must keep tracking their own original word for forced-
        # alignment timing to stay in sync (see Segment's docstring in
        # alignment.py and AlignmentResult's in this module).

        protected: List[Verbatim] = []

        def _repl(match, protected=protected):

            content = match.group(1)
            leading_ws = len(content) - len(content.lstrip())
            body_start = match.start(1) + leading_ws
            body = content.strip()
            body_end = body_start + len(body)

            decision = classify_parenthetical(
                content=content,
                preceding_text=tracker.text[:match.start()],
            )

            if decision.kind in (ParentheticalKind.INITIALISM, ParentheticalKind.CODE):
                # Captured NOW, while body_start/body_end still line up
                # with tracker.text -- restoration happens after later
                # dictionary/unit/symbol passes have already rewritten
                # the rest of the document, so any position captured now
                # is meaningless by then. Only the (text, origin) pair
                # itself survives the trip -- see restore_protected_
                # parentheticals().
                body_origin = tracker.origin[body_start:body_end]
                first_origin = body_origin[0] if body_origin else (0,)
                last_origin = body_origin[-1] if body_origin else (0,)

                index = len(protected)
                protected.append(Verbatim(
                    text=f" {body} ",
                    origins=(first_origin,) + tuple(body_origin) + (last_origin,),
                ))
                return [f" \x02PH{index}\x02 "]

            if not body:
                return [""]

            # PROSE: brackets become surrounding commas, but the body
            # itself is kept as a verbatim slice of the CURRENT text so
            # each of its words retains its own origin.
            trailing = "" if body[-1] in ".,;:!?" else ","

            return [", ", (body_start, body_end), f"{trailing} "]

        tracker.apply_segments(PARENTHETICAL_RE, _repl)

        return protected

    def restore_protected_parentheticals(self, tracker: AlignmentTracker, protected: List[Verbatim]):

        if not protected:
            return

        tracker.apply_segments(
            _PLACEHOLDER_RE,
            lambda m, p=protected: [p[int(m.group(1))]]
        )

    # -------------------------------------------------------
    # Protect reference/task codes
    #
    # A multi-segment hyphenated code like "79-24-00-870-7A" must not be
    # touched by any later dictionary/unit/symbol pass -- e.g.
    # replace_units' "\d+\s*unit\b" pattern could otherwise match a
    # digit+letter tail such as "7A" as a standalone quantity and
    # rewrite it as a unit ("7 amperes"). So, just like
    # replace_parentheticals above, this rewrites each matched code to
    # its final form up front and shields THAT result behind a
    # placeholder until every dictionary/unit/symbol pass has run.
    #
    # Unlike a plain protect-verbatim, the "final form" here is not the
    # code as authored -- it's converted to natural speech (see
    # _speak_reference_code above), because Piper reads a bare "-" as
    # the word "dash" and mangles a doubled "00" if the code reaches it
    # unchanged.
    #
    # This runs as its own pass over the WHOLE document -- these codes
    # show up in plain running text, not just inside brackets.
    # REFERENCE_CODE_RE requires 3+ hyphen-separated numeric segments
    # specifically so a genuine two-value unit range ("5mm-10mm") is
    # never caught by mistake: fixing this by instead teaching
    # replace_units to refuse a number immediately after a hyphen was
    # tried and rejected -- it also blocks the SECOND half of a
    # legitimate range from ever being expanded.
    # -------------------------------------------------------

    def protect_reference_codes(self, tracker: AlignmentTracker) -> List[Verbatim]:

        protected: List[Verbatim] = []

        def _repl(match, protected=protected):

            start, end = match.span()

            matched_origins = set()
            for i in range(start, end):
                matched_origins.update(tracker.origin[i])
            origin = tuple(sorted(matched_origins))

            spoken = _speak_reference_code(match.group(0))

            index = len(protected)
            protected.append(Verbatim(
                text=spoken,
                origins=(origin,) * len(spoken),
            ))
            # Pad with spaces: the code may have been carved out of the
            # middle of a larger glued token ("ALHMKMTM-SYSTEM-79-...")
            # and its spoken form ("seventy-nine, ...") must not fuse
            # onto the neighbouring word once restored. Redundant
            # whitespace is collapsed by cleanup().
            return [f" \x02RC{index}\x02 "]

        tracker.apply_segments(REFERENCE_CODE_RE, _repl)

        return protected

    def restore_protected_reference_codes(self, tracker: AlignmentTracker, protected: List[Verbatim]):

        if not protected:
            return

        tracker.apply_segments(
            _REFERENCE_CODE_PLACEHOLDER_RE,
            lambda m, p=protected: [p[int(m.group(1))]]
        )

    # -------------------------------------------------------
    # Replace Dictionary
    #
    # Applied against the AlignmentTracker so every substitution
    # keeps a record of which original word(s) it came from.
    # -------------------------------------------------------

    def replace_dictionary(self, tracker: AlignmentTracker, dictionary: dict):

        # Longest key first
        keys = sorted(
            dictionary.keys(),
            key=len,
            reverse=True
        )

        for key in keys:

            value = dictionary[key]

            pattern = re.compile(
                r"(?<!\w)"
                + re.escape(key)
                + r"(?!\w)",
                flags=re.IGNORECASE
            )

            tracker.apply(pattern, lambda m, v=value: v)

    # -------------------------------------------------------
    # Dimension abbreviations -> spoken words
    #
    # "L x W x H"  -> "Length by Width by Height"
    # "H: 25mm"    -> "Height: 25 millimetres"
    #
    # Runs AFTER the dictionary passes and AFTER replace_units. The bare
    # "L"/"W"/"H"/"D" keys were removed from ENGINEERING_TERMS precisely
    # because matching them standalone corrupted unit compounds and prose;
    # here they are only ever touched inside one of the two explicit
    # dimension shapes above.
    #
    # apply_segments() with a Verbatim per letter so each dimension word
    # inherits the origin of its own original letter token (the "x"
    # separators, which are not spoken, fall out as zero-width markers) --
    # the highlight then steps L -> W -> H cleanly.
    # -------------------------------------------------------

    def expand_dimension_abbreviations(self, tracker: AlignmentTracker) -> None:

        def _triple(match):
            segments: List = []
            prev_origin = None
            for group_index in (1, 2, 3):
                letter = match.group(group_index)
                if letter is None:
                    continue
                origin = tracker.origin[match.start(group_index)]
                if segments:
                    segments.append(Verbatim(
                        text=" by ",
                        origins=(prev_origin,) * len(" by "),
                    ))
                word = _DIMENSION_WORDS[letter.upper()]
                segments.append(Verbatim(text=word, origins=(origin,) * len(word)))
                prev_origin = origin
            return segments

        tracker.apply_segments(_DIMENSION_TRIPLE_RE, _triple)

        def _label(match):
            origin = tracker.origin[match.start(1)]
            word = _DIMENSION_WORDS[match.group(1).upper()]
            return [
                Verbatim(text=word, origins=(origin,) * len(word)),
                ":",
            ]

        tracker.apply_segments(_DIMENSION_LABEL_RE, _label)

    # -------------------------------------------------------
    # Normalize slash-joined words -> "word or word"
    #
    # A run of unspaced alphanumeric words joined by "/" ("task/document",
    # "inspection/check", "L/W/H") is shorthand for "or" and is spoken
    # that way, not as the bare word "slash" the symbol pass would emit.
    #
    # The pattern is deliberately narrow -- \b<alnum>(/<alnum>)+\b -- so it
    # leaves alone the slashes that are NOT an "or":
    #   - file paths / URLs: "https://x" ("//" has no alnum between the
    #     slashes), "a/b/" (trailing slash), "/etc/hosts" (leading slash)
    #   - the "/A" tail of a reference code like "79-24-00/A": by the
    #     time this runs the numeric part is already a \x02RC..\x02
    #     placeholder, so there is no alnum immediately left of the slash
    #
    # A run that is itself a unit ("km/h", "m/s", "ft/min") -- e.g. a bare
    # "km/h" with no leading number that replace_units could latch onto --
    # is expanded to the spoken unit name here instead of "or".
    #
    # Runs AFTER replace_units (so "60km/h" is already "60 kilometres per
    # hour") and BEFORE replace_symbols / expand_initialisms.
    #
    # The whole run is always a single \S+ original token (a slash with a
    # space on either side never matches), so apply() with the match's
    # union origin already maps every emitted word back to that one
    # original word -- no per-segment bookkeeping needed.
    # -------------------------------------------------------

    def normalize_slashes(self, tracker: AlignmentTracker) -> None:

        def _repl(match):
            run = match.group(0)
            unit = UNITS.get(run.lower())
            if unit is not None:
                return f" {unit} "
            return " or ".join(run.split("/"))

        tracker.apply(_SLASH_COMPOUND_RE, _repl)

    # -------------------------------------------------------
    # Replace Symbols
    # -------------------------------------------------------

    def replace_symbols(self, tracker: AlignmentTracker):

        # Longest symbol first
        keys = sorted(
            SYMBOLS.keys(),
            key=len,
            reverse=True
        )

        for symbol in keys:

            tracker.replace_literal(symbol, SYMBOLS[symbol])

    # -------------------------------------------------------
    # Replace Units
    #
    # 25mm
    # 25 mm
    # 220V
    # 220 V
    #
    # NOTE: a number embedded in a multi-segment hyphenated reference
    # code (e.g. task number "79-24-00-870-7A") could otherwise have its
    # trailing digit+letter tail ("7A") misread as a unit ("7 amperes").
    # That's handled upstream by protect_reference_codes() shielding the
    # whole code behind a placeholder before this ever runs -- NOT by a
    # guard in this regex, because any such guard (e.g. refusing to match
    # a number immediately after a hyphen) also breaks a genuine two-
    # value unit range like "5mm-10mm" (only the first side would expand).
    # -------------------------------------------------------

    def replace_units(self, tracker: AlignmentTracker):

        units = sorted(
            UNITS.keys(),
            key=len,
            reverse=True
        )

        for unit in units:

            replacement = UNITS[unit]

            pattern = re.compile(
                r"(\d+(?:\.\d+)?)\s*"
                + re.escape(unit)
                + r"\b",
                flags=re.IGNORECASE
            )

            tracker.apply(
                pattern,
                lambda m, r=replacement: m.group(1) + " " + r
            )

    # -------------------------------------------------------
    # Expand unpronounceable initialisms
    #
    # Runs AFTER all dictionary/unit/symbol passes so that known
    # abbreviations (RPM → "Revolutions Per Minute", ASSY → "Assembly",
    # etc.) are already handled and will not be touched here.
    #
    # Uses apply() rather than apply_segments() because the entire
    # uppercase run is one original-word token: every expanded letter
    # inherits the same origin so the forced-aligner's per-letter timings
    # all fold back into a single highlighted span in the DOM.
    #
    # The spelled-out form is padded with a space on each side so its
    # first/last letter can never fuse onto an adjacent hyphen or letter
    # when the run was glued to neighbouring text -- e.g.
    # "ALHMKMTM-SYSTEM-..." must yield ".. T M" + "SYSTEM", not a single
    # "M-SYSTEM" token the forced aligner can't place. cleanup() collapses
    # the redundant whitespace afterwards.
    # -------------------------------------------------------

    def expand_initialisms(self, tracker: AlignmentTracker) -> None:
        tracker.apply(
            _INITIALISM_RE,
            lambda m: (
                f" {' '.join(m.group(1))} "
                if _is_unpronounceable(m.group(1))
                else m.group(1)
            ),
        )

    # -------------------------------------------------------
    # "ABS" -> "A B S"
    #
    # See the _ABS_ACRONYM_RE module comment above for why this exists
    # and why it's unconditionally spaced letters rather than periods.
    #
    # apply(), not apply_segments(): every occurrence this matches --
    # bare ("ABS Module") or the literal text a restored "(ABS)"
    # parenthetical leaves behind -- is exactly one original \S+ token,
    # the same single-token-substitution shape as expand_roman_numeral_
    # list_markers() above. The three letters share one origin either
    # way, so the union-origin apply() computes is already correct.
    #
    # MUST run after restore_protected_parentheticals()/
    # restore_protected_reference_codes() in process_with_alignment(),
    # not among the other early/dictionary passes: "Anti-Lock Braking
    # System (ABS)" classifies as an INITIALISM parenthetical (A -> "
    # Anti-Lock", B -> "Braking", S -> "System" -- verified directly
    # against parentheticals.py's _is_initialism_of), so
    # replace_parentheticals() swaps "(ABS)" for a \x02PH..\x02
    # placeholder immediately -- there is literally no "ABS" substring in
    # tracker.text for this rule to find until that placeholder is
    # restored back to literal text, which doesn't happen until the very
    # end of the pipeline. Running this any earlier would silently miss
    # every bracketed "(ABS)" while still catching bare "ABS Module"
    # occurrences -- an inconsistency, not a clean fix.
    # -------------------------------------------------------

    def expand_abs_acronym(self, tracker: AlignmentTracker) -> None:
        tracker.apply(_ABS_ACRONYM_RE, lambda m: "A B S")

    # -------------------------------------------------------
    # Cleanup
    #
    # Whitespace-only normalization — never touches word tokens,
    # so it's safe to run after alignment groups are computed
    # without affecting the grouping itself.
    # -------------------------------------------------------

    def cleanup(self, text):

        # tighten space before punctuation introduced by parenthetical
        # rewriting, e.g. "... fuel levels , ." -> "... fuel levels."
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)

        # collapse a comma that ends up directly before terminal
        # punctuation, e.g. ",." -> "."
        text = re.sub(r",\s*([.;:!?])", r"\1", text)

        # remove repeated spaces
        text = re.sub(
            r"[ \t]+",
            " ",
            text
        )

        # preserve paragraph spacing
        text = re.sub(
            r"\n{3,}",
            "\n\n",
            text
        )

        return text.strip()

    # -------------------------------------------------------
    # Main Pipeline (alignment-aware)
    #
    # Returns an AlignmentResult containing:
    #   - processed_text    : fully expanded text, sent to Piper
    #   - original_words    : words as authored (what the browser
    #                         highlights)
    #   - processed_words   : words as actually spoken
    #   - groups            : original-word -> processed-word-range
    #                         mapping, used to build per-original-word
    #                         timings that stay in sync with the audio
    # -------------------------------------------------------

    def process_with_alignment(self, text) -> AlignmentResult:

        if not text:
            return AlignmentResult(
                processed_text="",
                original_words=[],
                processed_words=[],
                groups=[]
            )

        tracker = AlignmentTracker(text)

        #
        # Heading pause markers (see currentChapterText() in app.js) ->
        # real sentence-ending punctuation.
        #

        self.insert_heading_pauses(tracker)

        #
        # Roman-numeral list markers ("i.", "ii)", ...) -> spoken
        # cardinal numbers. Runs before every dictionary/unit/symbol pass
        # so none of them get a chance to touch a marker first.
        #

        self.expand_roman_numeral_list_markers(tracker)

        #
        # Standalone prose dashes -> comma pauses, so Piper doesn't read
        # them aloud as the word "dash". Only ever touches a dash with
        # whitespace on both sides, so compound words, hyphenated
        # numbers, unit ranges and reference codes are all unaffected.
        #

        self.normalize_standalone_dashes(tracker)

        #
        # Emoji / invisible-character stripping (TTS-bound copy only)
        #

        self.strip_tts_only_characters(tracker)

        #
        # Parentheticals (acronym/code output placeholder-protected)
        #

        protected_parentheticals = self.replace_parentheticals(tracker)

        #
        # Reference/task codes (e.g. "79-24-00-870-7A"), placeholder-
        # protected from every dictionary/unit/symbol pass below -- see
        # protect_reference_codes(). Runs after parentheticals so a code
        # written as "(Refer to Task 79-24-00-870-7A)" is protected from
        # its own resolved (bracket-free) position, not from inside the
        # not-yet-classified bracket span.
        #

        protected_reference_codes = self.protect_reference_codes(tracker)

        #
        # Units FIRST — before any dictionary pass.
        #
        # Unit compounds ("km/h", "m/s", "ft/min", "220V") are
        # unambiguous and must be expanded to full spoken names before a
        # dictionary substitution or the slash-normalizer can get at a
        # fragment of one. (This is also why the bare "L"/"W"/"H"
        # dimension keys were removed from ENGINEERING_TERMS.)
        #

        self.replace_units(tracker)

        #
        # Client Dictionary
        #

        self.replace_dictionary(
            tracker,
            self.client_dictionary
        )

        #
        # Engineering Terms
        #

        self.replace_dictionary(
            tracker,
            ENGINEERING_TERMS
        )

        #
        # General Abbreviations
        #

        self.replace_dictionary(
            tracker,
            ABBREVIATIONS
        )

        #
        # Dimension abbreviations ("L x W x H", "H: 25mm") — only inside
        # an explicit dimension pattern, never as standalone keys.
        #

        self.expand_dimension_abbreviations(tracker)

        #
        # slash-joined words -> "word or word" (before the symbol pass
        # turns a bare "/" into " slash ")
        #

        self.normalize_slashes(tracker)

        #
        # Symbols
        #

        self.replace_symbols(tracker)

        #
        # Initialisms — spell out unpronounceable uppercase sequences
        # letter-by-letter (e.g. "ALHMKMTM" -> "A L H M K M T M").
        # Runs after all dictionary passes so known acronyms are already
        # handled and will not be re-processed here.
        #

        self.expand_initialisms(tracker)

        #
        # Restore protected acronyms/codes/reference-numbers now that no
        # further dictionary, unit, or symbol pass can re-expand them.
        #

        self.restore_protected_parentheticals(tracker, protected_parentheticals)
        self.restore_protected_reference_codes(tracker, protected_reference_codes)

        #
        # "ABS" -> "A B S". Deliberately LAST, after both restores above
        # -- see expand_abs_acronym()'s own comment for why a bracketed
        # "(ABS)" doesn't exist as literal text to match until then.
        #

        self.expand_abs_acronym(tracker)

        #
        # Finalize: computes alignment groups, then cleans up
        # whitespace in the final processed text.
        #

        return tracker.finalize(cleanup_fn=self.cleanup)

    # -------------------------------------------------------
    # Backward-compatible plain-text pipeline
    #
    # Same substitutions as process_with_alignment(), just without
    # the alignment bookkeeping. Kept for any caller that only needs
    # the expanded text (e.g. a debug/preview endpoint) and doesn't
    # need word-level sync.
    # -------------------------------------------------------

    def process(self, text):

        return self.process_with_alignment(text).processed_text


preprocessor = TextPreprocessor()
