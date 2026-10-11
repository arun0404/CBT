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
from .text_sanitizer import NBSP_ENTITY_PATTERN, TTS_STRIP_PATTERN
from .parentheticals import (
    PARENTHETICAL_RE,
    PARENTHETICAL_CONFIG_VERSION,
    SPEAK_AS_WORD,
    SPELL_MAX_LETTERS,
    ParentheticalKind,
    classify as classify_parenthetical,
    spell_spans,
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

# And a third, for client_dictionary.json expansions (see
# replace_client_dictionary): an expansion is final text, so it is parked
# behind this placeholder while every later pass runs, then restored.
_CLIENT_PLACEHOLDER_RE = re.compile(r"\x02CD(\d+)\x02")

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
# Three written forms are list markers: "i."  "i)"  and "(i)".
#
# Case: lower-case is always a marker. Upper-case is a marker for the
# multi-letter numerals ("II." "IV)" "(III)"), but a single upper-case
# "I", "V" or "X" counts only with a closing parenthesis ("I)" "(V)"):
# followed by a period it is the pronoun "I" or a person's initial ("V.
# Rao", "X. Ray"), and sentence-final "I." is far more common in real
# prose than an upper-case list.
#
# Position: the dot and closing-paren forms are scoped to the START of a
# line (re.MULTILINE "^", i.e. right after a "<br>"-turned-newline or the
# very start of the text) specifically so an ordinary word or letter
# elsewhere in a sentence is never touched -- a variable at the end of a
# sentence ("find x."), "vs. i." mid-sentence, or "i.e." (excluded
# separately below, since that already starts a fresh clause after its own
# preceding period and so CAN land at a line start). A bare i, v or x with
# no marker punctuation is never touched anywhere. The trailing (?=\s|$)
# requires the marker to be followed by whitespace or end-of-string, which
# is what actually excludes "i.e." -- there is no space between "i." and
# "e".
#
# Inside a sentence only the fully parenthesised form counts, and only at
# the start of a clause (after ";" ":" "," "." or "and"/"or"): "do: (i)
# clean it; (ii) replace it". That position is what separates an enumerator
# from a variable named in brackets ("velocity (v)", "current (i)",
# "Voltage (V)", "f(x)"), which follows a noun and stays a letter. A mid-
# sentence "i)" is never touched: it is usually the end of a parenthetical
# ("(see step v)").
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
_ROMAN_LOWER = sorted(ROMAN_NUMERAL_LIST_WORDS.keys(), key=len, reverse=True)

_ROMAN_ALT_LOWER = "|".join(_ROMAN_LOWER)
_ROMAN_ALT_UPPER = _ROMAN_ALT_LOWER.upper()
_ROMAN_ALT_UPPER_MULTI = "|".join(k.upper() for k in _ROMAN_LOWER if len(k) > 1)

# At the start of a line: "i." / "i)" (any numeral, lower-case, or multi-
# letter upper-case), "(i)" (any numeral, either case), and "I)" / "V)" /
# "X)" (the single upper-case letters, closing parenthesis only).
ROMAN_LIST_MARKER_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?:"
    r"\((?P<paren>" + _ROMAN_ALT_LOWER + "|" + _ROMAN_ALT_UPPER + r")\)"
    r"|(?P<dot>" + _ROMAN_ALT_LOWER + "|" + _ROMAN_ALT_UPPER_MULTI + r")[.)]"
    r"|(?P<cap>I|V|X)\)"
    r")(?=\s|$)"
)

# In the middle of a sentence: a parenthesised numeral, set off from the word
# before it (so not "f(x)" or "3(i)") and followed by a space or by
# punctuation ("(i), (ii) and (iii)"). Lower-case, or multi-letter upper-case
# ("(IV)"); a single upper-case "(V)" / "(X)" is a unit or a label, never here.
#
# A multi-letter numeral ("(ii)", "(IV)") is always an enumerator. A single
# lower-case letter might instead be a variable named in brackets, which is
# decided in expand_roman_numeral_list_markers() from where it sits on its
# line.
ROMAN_INLINE_CANDIDATE_RE = re.compile(
    r"(?<![\w)\]])\((?P<num>" + _ROMAN_ALT_LOWER + "|" + _ROMAN_ALT_UPPER_MULTI
    + r")\)(?=[ \t]|[,;:.](?=\s|$)|$)"
)

# The numerals in order, to recognise a run: "(i) ... (ii) ... (iii)".
_ROMAN_SEQUENCE = list(ROMAN_NUMERAL_LIST_WORDS)

# What may stand directly before a clause-initial enumerator.
_CLAUSE_START_BEFORE_RE = re.compile(r"(?:[;:,.]|\b(?i:and|or))$")

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

# A dash standing alone at the very START or END of the text has whitespace
# on only one side, so the rule above (which needs both) misses it. It is
# simply dropped: with nothing on the other side there is nothing to pause
# between. (Piper ignores such a dash anyway; this just keeps the text it
# receives clean.) The captured whitespace is kept, so no word fuses.
_EDGE_DASH_RE = re.compile(r"\A(\s*)[-–—]+(?=\s)|(?<=\s)[-–—]+(\s*)\Z")

# A minus sign in front of a number is SPOKEN: "-50 °C" is "minus 50 degrees
# Celsius". Piper ignores a "-" glued to a number, so without this the sign
# was silently lost ("-0.5°" was read as "0.5 degrees").
#
# The sign has to be attached to the digits and to OPEN the number: at the
# start of the text, after whitespace, after an opening bracket, "=", "," or
# ";", or after a quote. (A number that starts with a bare point, "-.5", is
# left alone: the passes after this one already drop the space before a
# leading ".", which would read "minus.5" with a break after "minus".) (The look-behind is a
# negative one, "not preceded by anything but those", because a Python
# look-behind cannot be the alternation ^|\s -- its branches differ in width
# -- and a negative one also lets the very start of the text through.) That
# is what leaves everything else exactly as it was:
#
#   "10-20 mm"  "2024-10-11"  "F-16"  "ISO-9001"  reference codes
#       the hyphen follows a letter or digit: a joiner, never a sign
#   "Anti-Lock"  "Sub-system 3"
#       no digit follows
#   "- Standard practices"  "- 5 mm clearance"  "CAN – 500 kilobits"
#       a bullet or prose dash has a space after it. The manual really does
#       write "High-speed CAN — 500 kilobits per second"; reading that
#       dash as a sign would say "CAN minus 500 kilobits".
#
# Hyphen-minus, en dash and the true minus sign are accepted as the sign; the
# em dash never is (it is always a prose dash). Only the TRUE minus sign
# (U+2212) may also be followed by spaces ("− 50"): unlike a hyphen or an
# en dash it is never a bullet or a range dash.
NEGATIVE_NUMBER_RE = re.compile(
    r"(?<![^\s(\[{=,;\"'‘“])"
    r"(?:(?P<attached>[-–−])(?=\d)|−(?P<gap>[ \t]+)(?=\d))"
)

# A non-breaking space written as TEXT ("&nbsp;", "&nbsp", "&#160;"): see
# NBSP_ENTITY_PATTERN in text_sanitizer.py for exactly what matches and why.
_NBSP_ENTITY_RE = re.compile(NBSP_ENTITY_PATTERN)

# Unicode spaces other than the plain one. All of them are whitespace to
# both the browser's \s and Python's, so they never change where one word
# ends and the next begins; they are only normalised so the text handed to
# Piper contains ordinary spaces ("clean text").
_UNICODE_SPACE_RE = re.compile("[   -   　]")

# Where the browser and Python disagree about what is whitespace. The
# highlighter finds words with JavaScript's /\S+/, the backend with Python's
# \S+, and every word index after a disagreement is off by one. Across all
# 1.1 million code points exactly six differ:
#   U+FEFF            whitespace to JavaScript, an ordinary character to Python
#   U+001C-U+001E, U+0085   whitespace to Python, ordinary characters to JavaScript
# (U+001F is the heading-pause marker; being whitespace to Python is intended.)
_FEFF = "﻿"
_PYTHON_ONLY_WHITESPACE_RE = re.compile("[\x1c\x1d\x1e\x85]")


def normalize_dom_whitespace(text: str) -> str:
    """Make Python's word boundaries match the browser's for the six code
    points above, so original_words is index-for-index what the DOM wraps.

    U+FEFF becomes a space (the browser splits there); the control
    characters become a zero-width space, which keeps the word whole as the
    browser does and is stripped before Piper (see text_sanitizer.py).
    Lengths are unchanged for the controls; U+FEFF is one-for-one too.
    """

    if _FEFF in text:
        text = text.replace(_FEFF, " ")

    return _PYTHON_ONLY_WHITESPACE_RE.sub("​", text)

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

# A slash with whitespace on BOTH sides -- "Inspection / Maintenance",
# "SAFE / ARM", "12 months / 15,000 km" -- is an alternative and is spoken
# "or". Whitespace means any whitespace, newlines included: the text the
# browser posts has the words of a block on separate lines ("Primary\n/\n
# secondary"). Unspaced slashes are a different thing (a unit ratio, an
# identifier, a "task/document" shorthand) and are not touched here; a
# slash at the very start or end of the text, or against a word on one side
# only, has whitespace on one side at most and is left to the passes after.
#
# Only the slash is matched, not the spaces around it. Whitespace carries
# the origin of the word BEFORE it, so matching "\s+/\s+" would fold that
# word into the group of the "or" and the two could no longer be
# highlighted separately. The "/" is a word of its own in the text, so
# it keeps its own origin: the highlighter lights it while "or" is said.
_SPACED_SLASH_RE = re.compile(r"(?<=\s)/(?=\s)")

# "Page 3 / 10": two whole numbers either side of a spaced slash, after a word
# that numbers things in a series, are "3 out of 10", not "3 or 10".
#
# The word before is REQUIRED. A spaced slash between numbers is more often a
# pair of alternatives -- "Supply 50 / 60 Hz", "Use 110 / 220 V", "Fit 4 / 6
# bolts" -- and those must stay "or". Without a counter word there is no
# telling the two apart, so the general rule applies ("or").
#
# The numbers must be whole and stand alone: "Page 3.5 / 10", "Page 3 / 10.5",
# "Page 3 / 10-12" and "Page 3 / ten" are not counters. A closing bracket or
# sentence punctuation may follow the second number.
_COUNTER_WORDS = (
    "page", "pg", "p", "step", "item", "sheet", "slide", "question",
    "image", "photo", "picture", "chapter",
)
_NUMERIC_COUNTER_RE = re.compile(
    r"(?<![\w.])(?:" + "|".join(_COUNTER_WORDS) + r")\.?\s+\d+\s+(?P<slash>/)\s+\d+"
    r"(?![A-Za-z0-9/-])(?!\.\d)(?!,\d)",
    re.IGNORECASE,
)

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

# The full "L x W x H" triple in ANY letter case ("l x w x h", "l × W × h").
# _DIMENSION_TRIPLE_RE above stays upper-case-only for the two-letter
# forms ("H x W"); a lower-case run is only accepted when it is the
# complete length-width-height sequence, which is unambiguous.
_DIMENSION_LWH_ANYCASE_RE = re.compile(
    r"\b([Ll])\s*[xX×]\s*([Ww])\s*[xX×]\s*([Hh])\b"
)

# --------------------------------------------------------------------
# "L" / "l" is ambiguous: LITRES (volume) or LENGTH (a dimension). A flat
# dictionary can't tell them apart, so each reading is only recognised in
# an explicit context and a bare "L" is otherwise left alone.
# --------------------------------------------------------------------

# LITRES -- a number followed by L. Thousands groups ("1,200") are taken
# whole so ",001" is not mistaken for the value 1 (singular).
_LITRE_NUMBER = r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"

# "2.0L", "50 L", "1 l". The lookbehind refuses to start mid-token or
# mid-number ("M10L", "v1.5L", the tail of "1,5"), so a part number is
# never read as a volume. The optional named group is an empty lookahead
# that is set only when an engine noun follows ("2.0 L engine"): there the
# unit is a compound modifier and is singular, like "a two litre engine".
_LITRE_AMOUNT_RE = re.compile(
    r"(?<![\w.,])" + _LITRE_NUMBER + r"\s*([Ll])(?!\w)"
    r"(?P<engine>(?=\s+(?:engine|motor|petrol|diesel|turbo(?:charged)?)\b))?",
    re.IGNORECASE,
)

# The same, but only directly inside brackets: "(50L)", "(1.6 L)". A
# one-token bracket such as "(50L)" otherwise looks like a part code to
# the parenthetical pass, which shields it from every later unit rule and
# lets Piper read the bare letter. Run BEFORE that pass.
_LITRE_BRACKETED_RE = re.compile(
    r"(?<=\()\s*" + _LITRE_NUMBER + r"\s*([Ll])(?=\s*\))"
)

# Fuel consumption "L/100km", "l/100 km". Must run before replace_units
# and normalize_slashes, which would otherwise turn the slash into "or".
_LITRES_PER_100KM_RE = re.compile(r"(?<!\w)[Ll]\s*/\s*100\s*km(?!\w)", re.IGNORECASE)

# Litres per minute / hour: "L/min", "l/h", "L/hr". Same reason as above (it
# has to run before replace_units and normalize_slashes, which would say
# "litres or h"). The unit part is CASE-SENSITIVE and lower-case only, so
# "L/H" (Left Hand) and "L/S" are never touched.
_LITRES_RATE_RE = re.compile(r"(?<!\w)[Ll]\s*/\s*(min|hrs|hr|h)(?!\w)")
_RATE_PERIOD = {"min": "minute", "hrs": "hour", "hr": "hour", "h": "hour"}

# A bearing: a number, a degree sign, then a compass letter ("12°N", "77.5 °E").
# Upper-case letter only, so "°C"/"°F" and ordinary text never match.
_DEGREE_COMPASS_RE = re.compile(r"(?<![\w.,])(\d+(?:\.\d+)?)\s*°\s*([NSEW])(?!\w)")
_COMPASS_WORDS = {"N": "North", "S": "South", "E": "East", "W": "West"}

# LENGTH -- "L = 250 mm" (a variable being assigned). "==", "<=", ">=" are
# not assignments: the lookahead insists on a single "=" directly after
# the letter.
_LENGTH_ASSIGN_RE = re.compile(r"(?<!\w)[Ll](?=\s*=(?!=))")

# "Length L = ..." -- "length" is already spoken, so don't say it twice.
_ENDS_WITH_LENGTH_RE = re.compile(r"\blength\s+\Z", re.IGNORECASE)

# LENGTH -- the symbol L given in brackets after the thing it measures.
_LENGTH_SYMBOL_RE = re.compile(
    r"\b(wheelbase|overall\s+length|crack\s+length)(\s*)(\(\s*[Ll]\s*\))",
    re.IGNORECASE,
)

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


def _spell_in_place(body: str, body_origin, spans):
    """`body` with each (start, end) span read letter by letter
    ("IADS" -> "I A D S"), plus a matching per-character origin tuple.

    Every letter keeps the origin of the character it came from, and each
    inserted space takes the origin of the letter before it, so the spelled
    word still maps back to the one original word it was written as -- the
    browser's highlight and the forced-alignment timings depend on that.
    Text outside the spans is carried through untouched.
    """

    text: List[str] = []
    origins: list = []
    cursor = 0

    for start, end in spans:

        text.append(body[cursor:start])
        origins.extend(body_origin[cursor:start])

        for i in range(start, end):
            if i > start:
                text.append(" ")
                origins.append(body_origin[i - 1])
            text.append(body[i])
            origins.append(body_origin[i])

        cursor = end

    text.append(body[cursor:])
    origins.extend(body_origin[cursor:])

    return "".join(text), tuple(origins)


class TextPreprocessor:

    def __init__(self):

        self.client_dictionary = self.load_client_dictionary()

        # Two patterns, applied at two points of the pipeline (see
        # replace_client_dictionary): COMPOUND keys -- those containing "/"
        # or "." ("kg/hr", "g/kW.h", "L/L", "N.m") -- run FIRST, before any
        # unit or single-letter rule can take a piece of them; every other
        # key runs after the unit pass, as it always has.
        self._client_compound_re = self._compile_case_sensitive({
            k: v for k, v in self.client_dictionary.items() if self._is_compound_key(k)
        })
        self._client_dictionary_re = self._compile_case_sensitive({
            k: v for k, v in self.client_dictionary.items() if not self._is_compound_key(k)
        })

        # Every abbreviation the case-insensitive unit and engineering tables
        # expand, keyed by its lower-cased form -- used to tell a known
        # abbreviation ("FIG", "KG") from an unknown bracketed acronym
        # ("IADS") when deciding what to spell letter by letter. Later table
        # wins; only the expansion matters.
        #
        # ABBREVIATIONS is deliberately NOT in here: its keys are Title-case
        # ("Dr", "Co", "Inc") and only match an ALL-CAPS word because that
        # pass ignores case. "(CO)" in brackets is carbon monoxide, not
        # "Company", so it must still be spelled.
        self._known_expansions = {}
        for table in (UNITS, ENGINEERING_TERMS):
            for key, value in table.items():
                self._known_expansions[key.casefold()] = value

        # Multi-word dictionary keys ("AIR COND", "TGT / T45", "SUB ASSY"). A
        # bracketed word that belongs to one is never spelled: that would
        # break the key, which is matched as a whole phrase.
        engineering_phrases = sorted(
            (k for k in ENGINEERING_TERMS if re.search(r"\s", k)), key=len, reverse=True
        )
        self._phrase_patterns = [
            self._compile_case_sensitive({
                k: v for k, v in self.client_dictionary.items() if re.search(r"\s", k)
            }),
            re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(k) for k in engineering_phrases) + r")(?!\w)",
                re.IGNORECASE,
            ) if engineering_phrases else None,
        ]

        # The unit ratios the compound passes can already say when they are
        # written without spaces. A spaced "km / h" is joined back into one of
        # them (see normalize_spaced_unit_ratios).
        self._spaced_unit_ratio_re = self._compile_spaced_unit_ratios()

        # UNITS' own ratios ("km/h", "kg/min", "°/s"), expanded as whole
        # tokens at the start of pass 1 (see expand_unit_ratios).
        self._unit_ratio_re = self._compile_unit_ratios()
        self._unit_ratio_expansions = {
            key.lower(): value for key, value in UNITS.items() if key.count("/") == 1
        }

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
    # Case-sensitive dictionary matching (client_dictionary.json)
    #
    # The client dictionary is matched EXACTLY as written: no
    # re.IGNORECASE, and no lower-casing of either side. Case is part of
    # an entry's identity -- "CBS" (Chip Burning System) and "CBs"
    # (Circuit Breakers), "MFDS" (Main Flexible Drive Shaft) and "MFDs"
    # (Multi Function Displays) are different terms that differ only in
    # case, and a case-folded lookup cannot keep them apart. It also
    # stops a key from firing on an ordinary word: "RAM" no longer
    # rewrites the "ram" in "ram air".
    #
    # ENGINEERING_TERMS and ABBREVIATIONS are deliberately NOT affected;
    # they are documented as case-insensitive and still go through
    # replace_dictionary(). A term listed in both places (e.g. "CAM") is
    # therefore still caught, in any case, by the engineering pass.
    #
    # One alternation, compiled once, rather than a pattern per key:
    #   * longest key first, so a short key can never take the front of
    #     a longer one ("SUB ASSY" before "SUB");
    #   * a single left-to-right pass, so an expansion is never
    #     re-scanned and re-expanded by a later key;
    #   * (?<!\w)/(?!\w) rather than \b -- identical for keys made of
    #     word characters (all of them today) but still correct for a
    #     key that starts or ends in punctuation ("No.").
    # Plurals and substrings therefore never match: "CBs" does not fire
    # inside "CBss" or "xCBs".
    #
    # An expansion is FINAL text. It is swapped for a \x02CD<n>\x02
    # placeholder while the later passes run and restored after them, so
    # nothing downstream can rewrite it. Without that, the later
    # case-insensitive dictionaries corrupted the entry's own words
    # ("Turn Co-ordination" -> "Turn Company-ordination", "No Picture In
    # Picture" -> "Number Picture In Picture", "Aircraft ID ..." -> "Aircraft
    # Inner Diameter ...") and the slash pass split "Latitude/Longitude"
    # into "Latitude or Longitude". The expansion is therefore spoken
    # exactly as written, with two exceptions applied here: "&" and "+" are
    # voiced as "and" / "plus" (the symbol table's own words), because
    # nearly every "Health & Usage ..." style entry relies on it. Anything
    # else (a slash, "RPM") must be written out in the entry itself.
    # -------------------------------------------------------

    @staticmethod
    def _compile_case_sensitive(dictionary: dict):

        if not dictionary:
            # An empty alternation would match the empty string at every
            # position, so "no entries" must mean "no pattern".
            return None

        keys = sorted(dictionary, key=lambda key: (-len(key), key))

        return re.compile(
            r"(?<!\w)(?:"
            + "|".join(re.escape(key) for key in keys)
            + r")(?!\w)"
        )

    @staticmethod
    def _is_compound_key(key: str) -> bool:
        """A key with an internal "/" or "." ("kg/hr", "g/kW.h", "L/L", "N.m").

        These are matched as whole tokens BEFORE the unit and single-letter
        rules: the unit pass would otherwise read the "kg" of "45 kg/hr" and
        the "g" of "210 g/kW.h" on its own, and the slash step would then turn
        what is left into "or"/"slash". Keys without such punctuation ("MW",
        "ms", "hr") deliberately stay AFTER the unit pass, so "5 MW" is still
        megawatts and not the dictionary's "Master Warning".
        """
        return "/" in key or "." in key

    def _compile_spaced_unit_ratios(self):
        """The pattern for a unit ratio written with spaces round its slash
        ("km / h", "kg / hr", "g / kW.h"), or None if there is none.

        Only ratios the compound passes ALREADY expand when written without
        spaces are listed -- UNITS' own ("km/h", "m/s") and the client
        dictionary's compound keys whose expansion is a rate ("kg/hr" ->
        "kilograms per hour") -- so joining one back together always lands on
        an entry that exists. A blanket "unit / unit" rule would be wrong:
        "N / A" is not newtons per ampere, and "min / max" is not a rate. A
        ratio the tables do not hold is not touched here and is read like any
        other spaced slash; add it to UNITS to give it a reading.

        UNITS keys match without regard to case, as they do everywhere else;
        client keys match exactly, as they do everywhere else.
        """
        alternatives = {}

        for key in UNITS:
            if key.count("/") == 1:
                left, right = (re.escape(part) for part in key.split("/"))
                alternatives[f"(?i:{left}\\s+/\\s+{right})"] = len(key)

        for key, value in self.client_dictionary.items():
            if key.count("/") == 1 and not re.search(r"\s", key) and " per " in value:
                left, right = (re.escape(part) for part in key.split("/"))
                alternatives[f"{left}\\s+/\\s+{right}"] = len(key)

        if not alternatives:
            return None

        ordered = sorted(alternatives, key=lambda pattern: -alternatives[pattern])

        return re.compile(
            r"(?<![A-Za-z./-])(?:" + "|".join(ordered) + r")(?![A-Za-z0-9/-])(?!\.\w)"
        )

    def _compile_unit_ratios(self):
        """The pattern for one of UNITS' ratio keys ("km/h", "kg/min", "°/s")
        written without spaces, or None if there are none.

        Matched without regard to case, as every UNITS key is, and only as a
        whole token: not inside a longer word, path or ratio ("dir/deg/s",
        "kg/min/m"). A digit directly before is allowed ("5kg/min").
        """
        keys = sorted(
            (key for key in UNITS if key.count("/") == 1),
            key=lambda key: (-len(key), key),
        )

        if not keys:
            return None

        return re.compile(
            r"(?<![A-Za-z./-])(?:" + "|".join(re.escape(key) for key in keys) + r")(?![A-Za-z0-9/-])(?!\.\w)",
            re.IGNORECASE,
        )

    def expand_unit_ratios(self, tracker: AlignmentTracker, protected: List[Verbatim]) -> None:
        """UNITS' ratios ("km/h", "kg/min", "°/s") -> "kilometres per hour",
        ... , in pass 1 right after the client compound keys.

        This is the pass that makes a ratio work when NO number precedes it,
        which replace_units (it needs "<number><unit>") cannot. Without it,
        "Climb rate (ft/min)" was read "Feet slash Minimum": the dictionary
        passes rewrote "ft" and "min" separately before the slash step saw
        the ratio. The expansion is parked behind the same placeholder as a
        client compound key, so no later pass can touch it, and is restored
        with them.
        """

        if self._unit_ratio_re is None:
            return

        def _repl(match):

            start, end = match.span()

            matched_origins = set()
            for i in range(start, end):
                matched_origins.update(tracker.origin[i])
            origin = tuple(sorted(matched_origins))

            spoken = self._speakable_expansion(self._unit_ratio_expansions[match.group(0).lower()])

            protected.append(Verbatim(
                text=spoken,
                origins=(origin,) * len(spoken),
            ))

            # "5kg/min": keep the number from fusing onto the first word.
            gap = " " if start > 0 and match.string[start - 1].isdigit() else ""

            return [f"{gap}\x02CD{len(protected) - 1}\x02"]

        tracker.apply_segments(self._unit_ratio_re, _repl)

    @staticmethod
    def _speakable_expansion(expansion: str) -> str:

        for symbol in ("&", "+"):
            expansion = expansion.replace(symbol, SYMBOLS[symbol])

        return re.sub(r"\s+", " ", expansion).strip()

    def replace_client_dictionary(
        self,
        tracker: AlignmentTracker,
        protected: List[Verbatim],
        *,
        compound: bool,
    ) -> None:
        """One client-dictionary pass. Expansions are appended to `protected`
        (shared by both passes, so placeholder numbers never repeat) and
        restored by restore_protected_client_dictionary()."""

        pattern = self._client_compound_re if compound else self._client_dictionary_re

        if pattern is None:
            return

        dictionary = self.client_dictionary

        # Applied against the AlignmentTracker (not a bare pattern.sub())
        # so each substitution keeps its record of which original word(s)
        # it came from -- the browser highlight and the forced-alignment
        # timings are built from that mapping. The expansion keeps the
        # union origin of the match, exactly as a plain apply() would give it.
        def _repl(match, protected=protected):

            start, end = match.span()

            matched_origins = set()
            for i in range(start, end):
                matched_origins.update(tracker.origin[i])
            origin = tuple(sorted(matched_origins))

            spoken = self._speakable_expansion(dictionary[match.group(0)])

            protected.append(Verbatim(
                text=spoken,
                origins=(origin,) * len(spoken),
            ))

            return [f"\x02CD{len(protected) - 1}\x02"]

        tracker.apply_segments(pattern, _repl)

    def restore_protected_client_dictionary(self, tracker: AlignmentTracker, protected: List[Verbatim]):

        if not protected:
            return

        tracker.apply_segments(
            _CLIENT_PLACEHOLDER_RE,
            lambda m, p=protected: [p[int(m.group(1))]]
        )

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
                # Which bracketed ALL-CAPS words are spelled letter by
                # letter: editing the word list or the length limit must
                # not leave stale cached audio behind.
                "bracket_spelling": [sorted(SPEAK_AS_WORD), SPELL_MAX_LETTERS],
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

        # The spoken word takes the origin of the MARKER's own first
        # character, not of the indentation before it: indentation inherits
        # the origin of the previous line's last word, and a union with that
        # would tie the marker's timing to the word before it.
        def _line_start(match):
            numeral = match.group("paren") or match.group("dot") or match.group("cap")
            spoken = ROMAN_NUMERAL_LIST_WORDS[numeral.lower()] + "."
            origin = tracker.origin[match.end("indent")]
            return [Verbatim(text=spoken, origins=(origin,) * len(spoken))]

        tracker.apply_segments(ROMAN_LIST_MARKER_RE, _line_start)

        # "do: (i) clean it; (ii) replace it" -> "do: One, clean it; Two,
        # replace it". A comma, not a full stop: a full stop would make each
        # enumerator its own sentence and Piper adds a half-second silence
        # after every sentence, which mid-sentence is far too choppy.
        # A parenthesised numeral in the middle of a line is an enumerator,
        # not a variable ("velocity (v)", "current (i)"), when EITHER
        #   * it starts a clause: after ";" ":" "," "." or "and" / "or"; or
        #   * it belongs to a run: its neighbour in the sequence ((ii) for
        #     (i), (i) or (iii) for (ii), ...) is also in brackets on the
        #     same line -- which is what catches the first marker of "are
        #     (i) visual, (ii) functional and (iii) electrical", where a
        #     plain word precedes it.
        # A lone "(v)" after a noun satisfies neither and stays a letter.
        def _inline(match):

            text = tracker.text
            numeral = match.group("num").lower()

            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line_end = len(text) if line_end == -1 else line_end

            before = text[line_start:match.start()].rstrip(" \t")

            # Is it a LABEL ("(i) clean the filter")? Yes if it starts a
            # clause, or belongs to a run of numbers on the line.
            is_label = before == "" or bool(_CLAUSE_START_BEFORE_RE.search(before))

            if not is_label:

                on_line = {
                    m.group("num").lower()
                    for m in ROMAN_INLINE_CANDIDATE_RE.finditer(text, line_start, line_end)
                }

                position = _ROMAN_SEQUENCE.index(numeral)
                neighbours = {
                    _ROMAN_SEQUENCE[i]
                    for i in (position - 1, position + 1)
                    if 0 <= i < len(_ROMAN_SEQUENCE)
                }

                is_label = bool(on_line & neighbours)

            # A single letter (i, v, x) could be a variable, so it needs to be
            # a label; "(ii)" or "(IV)" never is one, and after a noun is a
            # reference: "Part (IV)" -> "Part Four".
            if len(numeral) == 1 and not is_label:
                return [(match.start(), match.end())]              # a letter: leave it

            # Only a label gets the pause, and none if punctuation already
            # follows the number.
            followed_by_space = text[match.end():match.end() + 1] in (" ", "\t")

            spoken = ROMAN_NUMERAL_LIST_WORDS[numeral] + ("," if is_label and followed_by_space else "")
            origin = tracker.origin[match.start()]                  # the "("
            return [Verbatim(text=spoken, origins=(origin,) * len(spoken))]

        tracker.apply_segments(ROMAN_INLINE_CANDIDATE_RE, _inline)

    # -------------------------------------------------------
    # Negative numbers: "-50" -> "minus 50"
    #
    # See NEGATIVE_NUMBER_RE for exactly which signs count. Runs before
    # normalize_standalone_dashes() and the symbol passes, which would
    # otherwise drop or ignore the sign.
    #
    # "-50" is ONE original word, so the spoken "minus" simply belongs to it
    # along with the digits. "− 50" (a true minus sign, a space, a number) is
    # TWO original words: the sign word keeps a blank slot of its own, so it
    # stays a silent, never-highlighted word exactly like any other lone
    # dash (text_sanitizer.is_non_spoken_word, highlighter.js
    # isSpokenWord), and "minus" is spoken as part of the number's word.
    # -------------------------------------------------------

    def expand_negative_numbers(self, tracker: AlignmentTracker) -> None:

        def _repl(match):
            sign_origin = tracker.origin[match.start()]

            if match.group("gap") is None:
                return [Verbatim(text="minus ", origins=(sign_origin,) * 6)]

            number_origin = tracker.origin[match.end()]
            return [
                Verbatim(text=" ", origins=(sign_origin,)),
                (match.start("gap"), match.end("gap")),
                Verbatim(text="minus ", origins=(number_origin,) * 6),
            ]

        tracker.apply_segments(NEGATIVE_NUMBER_RE, _repl)

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

        # The edge rule goes first, so a dash that opens or closes the text
        # is dropped instead of becoming a stray leading/trailing comma.
        tracker.apply(_EDGE_DASH_RE, lambda m: (m.group(1) or "") + (m.group(2) or ""))

        tracker.apply(_STANDALONE_DASH_RE, lambda m: ", ")

    # -------------------------------------------------------
    # "&nbsp;" written as text -> a space
    #
    # A real entity never gets here: the browser has already turned it into
    # U+00A0 (handled below). This is for entity text that arrives AS text --
    # shown literally on the page, or inserted with textContent -- which
    # Piper would otherwise read aloud ("Engine System and nbsp;").
    #
    # It changes the SPOKEN text only. Original words are fixed before any
    # pass runs, so "10&nbspscrews" stays one word to the browser and to the
    # timings, and the two spoken words ("10 screws") simply share its group;
    # "Engine System&nbsp;" loses its tail; a word that was nothing but the
    # entity has no spoken word left and is a silent, never-highlighted one.
    # Splitting the word in the browser as well would put the two sides out of
    # step -- which is why highlighter.js leaves the words alone.
    #
    # Runs first, so no other pass ever sees the entity (the symbol pass would
    # voice its "&" as "and"). The surrounding spaces it leaves are collapsed
    # by cleanup().
    # -------------------------------------------------------

    def decode_nbsp_entities(self, tracker: AlignmentTracker) -> None:

        if "&" in tracker.text:
            tracker.apply(_NBSP_ENTITY_RE, lambda m: " ")

    # -------------------------------------------------------
    # Unicode spaces -> plain spaces (NBSP and friends), one character for
    # one character, so every origin stays exactly where it was.
    # -------------------------------------------------------

    def normalize_unicode_spaces(self, tracker: AlignmentTracker) -> None:

        if _UNICODE_SPACE_RE.search(tracker.text):
            tracker.apply(_UNICODE_SPACE_RE, lambda m: " ")

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
    # and SHOULD still have word and unit abbreviations inside it expanded
    # normally, e.g. "(see FIG 3)" -> "(see Figure 3)". An ACRONYM in it
    # ("(DC Power)", "(after checking the ECU)") is the exception: it is
    # spelled, and parked behind a placeholder like the INITIALISM / CODE
    # output, so it is read "D C Power" and not "Direct Current Power" --
    # see spell_spans().
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

            preceding_text = tracker.text[:match.start()]

            decision = classify_parenthetical(
                content=content,
                preceding_text=preceding_text,
            )

            # Which ALL-CAPS words in here are read letter by letter
            # ("(IADS)" -> "I A D S"): see spell_spans() for the rules.
            spans = spell_spans(
                body,
                decision.kind,
                preceding_text,
                self._bracket_expansion,
                self._keep_spans(body, decision.kind),
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
                spoken_body, spoken_origin = _spell_in_place(body, body_origin, spans)
                first_origin = spoken_origin[0] if spoken_origin else (0,)
                last_origin = spoken_origin[-1] if spoken_origin else (0,)

                index = len(protected)
                protected.append(Verbatim(
                    text=f" {spoken_body} ",
                    origins=(first_origin,) + spoken_origin + (last_origin,),
                ))
                return [f" \x02PH{index}\x02 "]

            if not body:
                return [""]

            # PROSE: brackets become surrounding commas, but the body
            # itself is kept as a verbatim slice of the CURRENT text so
            # each of its words retains its own origin. A word to be
            # spelled is parked behind its own placeholder (restored after
            # every later pass), so a unit or dictionary rule cannot touch
            # the spelled letters -- "(see 5 AB)" must not become
            # "5 amperes B".
            trailing = "" if body[-1] in ".,;:!?" else ","

            segments = [", "]
            cursor = body_start

            for start, end in spans:

                if body_start + start > cursor:
                    segments.append((cursor, body_start + start))

                spelled, spelled_origin = _spell_in_place(
                    body[start:end],
                    tracker.origin[body_start + start:body_start + end],
                    [(0, end - start)],
                )

                index = len(protected)
                protected.append(Verbatim(text=spelled, origins=spelled_origin))
                segments.append(f"\x02PH{index}\x02")

                cursor = body_start + end

            if cursor < body_end:
                segments.append((cursor, body_end))

            segments.append(f"{trailing} ")

            return segments

        tracker.apply_segments(PARENTHETICAL_RE, _repl)

        return protected

    def _keep_spans(self, body: str, kind) -> list:
        """Spans of a bracket's text that belong to a dictionary key spanning
        several words, and so must not have any of their words spelled.

        In a PROSE bracket the text goes on through the dictionary passes, so a
        compound key ("RAM/RADALT") is protected too; in a CODE bracket
        nothing is expanded, so only multi-word labels ("AIR COND") are.
        """

        patterns = [p for p in self._phrase_patterns if p is not None]

        if kind is ParentheticalKind.PROSE and self._client_compound_re is not None:
            patterns.append(self._client_compound_re)

        return [m.span() for p in patterns for m in p.finditer(body)]

    def _bracket_expansion(self, word: str):
        """What the pipeline's dictionaries would turn `word` into, or None.

        The client dictionary is matched exactly (it is case-sensitive); the
        others case-insensitively, as they are everywhere else.
        """

        expansion = self.client_dictionary.get(word)

        if expansion is not None:
            return expansion

        return self._known_expansions.get(word.casefold())

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
        tracker.apply_segments(_DIMENSION_LWH_ANYCASE_RE, _triple)

        def _label(match):
            origin = tracker.origin[match.start(1)]
            word = _DIMENSION_WORDS[match.group(1).upper()]
            return [
                Verbatim(text=word, origins=(origin,) * len(word)),
                ":",
            ]

        tracker.apply_segments(_DIMENSION_LABEL_RE, _label)

        # "L = 250 mm" -> "length equals 250 millimetres". Only the
        # letter is replaced; the "=" is still literal here and is spoken
        # by the symbol pass, each keeping its own original word. Left as
        # the bare letter after the word "length" ("Length L = 250"),
        # which would otherwise be said twice.
        def _length_variable(match):
            if _ENDS_WITH_LENGTH_RE.search(tracker.text, 0, match.start()):
                return match.group(0)
            return "length"

        tracker.apply(_LENGTH_ASSIGN_RE, _length_variable)

    # -------------------------------------------------------
    # "L" as LITRES
    #
    # "2.0L" / "50 L"  -> "2.0 litres" / "50 litres"
    # "1 L", "1.0 L"   -> "1 litre"          (exactly one is singular)
    # "2.0 L engine"   -> "2.0 litre engine" (compound modifier)
    # "8.5 L/100km"    -> "8.5 litres per hundred kilometres"
    #
    # Runs BEFORE replace_units, which would read the "100km" of
    # "L/100km" first and leave normalize_slashes to say "litres or 100
    # kilometres". replace_units still knows "L"/"l" as a fallback for the
    # odd shape this does not recognise (always plural, as before).
    #
    # The number keeps its own original-word origin and only the unit is
    # replaced, so "50 L" highlights "50" and "L" separately instead of
    # smearing both over one timing window.
    # -------------------------------------------------------

    def _litre_segments(self, tracker: AlignmentTracker, match) -> List:

        value = float(match.group(1).replace(",", ""))

        singular = (
            value == 1
            or match.groupdict().get("engine") is not None
        )

        word = "litre" if singular else "litres"
        origin = tracker.origin[match.start(2)]

        return [
            (match.start(1), match.end(1)),
            " ",
            Verbatim(text=word, origins=(origin,) * len(word)),
        ]

    def expand_litre_compounds(self, tracker: AlignmentTracker) -> None:
        """Compound litre tokens ("L/100km", "L/h", "L/min"): part of pass 1,
        right after the compound client-dictionary keys and before any
        single-letter rule."""

        tracker.apply(
            _LITRES_PER_100KM_RE,
            lambda m: "litres per hundred kilometres"
        )

        tracker.apply(
            _LITRES_RATE_RE,
            lambda m: f"litres per {_RATE_PERIOD[m.group(1)]}"
        )

    def expand_litres(self, tracker: AlignmentTracker) -> None:
        """A number followed by L ("2.0L", "50 L"): pass 2, single-letter rule."""

        tracker.apply_segments(
            _LITRE_AMOUNT_RE,
            lambda m: self._litre_segments(tracker, m)
        )

    # -------------------------------------------------------
    # Compass bearings: "12°N 77°E" -> "12 degrees North 77 degrees East"
    #
    # Without this the symbol pass says "12 degrees N". Only an upper-case
    # N/S/E/W directly after a degree sign counts, so "°C" / "°F" (handled
    # by the unit tables) and prose are untouched. The number keeps its own
    # original-word origin; the whole "12°N" is usually ONE original token, in
    # which case every spoken word simply maps back to it.
    # -------------------------------------------------------

    def expand_compass_degrees(self, tracker: AlignmentTracker) -> None:

        def _repl(match):
            word = _COMPASS_WORDS[match.group(2)]
            origin = tracker.origin[match.start(2)]
            return [
                (match.start(1), match.end(1)),
                " degrees ",
                Verbatim(text=word, origins=(origin,) * len(word)),
            ]

        tracker.apply_segments(_DEGREE_COMPASS_RE, _repl)

    def expand_bracketed_litres(self, tracker: AlignmentTracker) -> None:

        tracker.apply_segments(
            _LITRE_BRACKETED_RE,
            lambda m: self._litre_segments(tracker, m)
        )

    # -------------------------------------------------------
    # The symbol "L" in brackets after what it measures
    #
    # "wheelbase (L)"      -> "wheelbase length"
    # "overall length (L)" -> "overall length"   (already says length)
    #
    # Runs BEFORE replace_parentheticals, which would otherwise turn
    # "(L)" into ", L," and have Piper read the letter. The leading
    # words are carried through untouched (with their own origin); only
    # the bracketed symbol is rewritten, so it stays its own word.
    # -------------------------------------------------------

    def expand_length_symbols(self, tracker: AlignmentTracker) -> None:

        def _repl(match):

            phrase = (match.start(1), match.end(1))

            if match.group(1).lower().endswith("length"):
                return [phrase]

            origin = tracker.origin[match.start(3)]

            return [
                phrase,
                Verbatim(text=" length", origins=(origin,) * len(" length")),
            ]

        tracker.apply_segments(_LENGTH_SYMBOL_RE, _repl)

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
    # Spaced slash -> "or"
    #
    # See _SPACED_SLASH_RE. Runs after the compound-token and unit passes,
    # so a dictionary key that itself contains a spaced slash ("TGT / T45"),
    # a litre compound ("5 L / min") or a unit has already been expanded
    # (those expansions are protected placeholders, which hold no slash), and
    # before normalize_slashes() and replace_symbols(), the latter of which
    # would say "slash".
    #
    # The slash is its own \S+ token in the original text, so apply()'s
    # union origin is exactly that one original word.
    # -------------------------------------------------------

    def normalize_spaced_slashes(self, tracker: AlignmentTracker) -> None:

        tracker.apply(_SPACED_SLASH_RE, lambda m: "or")

    # -------------------------------------------------------
    # Spaced unit ratio -> the unspaced unit ratio: "50 km / h" -> "50 km/h"
    #
    # See _compile_spaced_unit_ratios for exactly which ratios. Runs BEFORE
    # the compound-token pass, not after it: the point is that the joined
    # form is then expanded by that pass ("kg/hr" -> "kilograms per hour"),
    # and a "kg/hr" made afterwards would never meet it and would be read as
    # "kilograms slash Hour" -- the very reading that pass exists to prevent.
    #
    # The three original words ("kg", "/", "hr") become one token, so they
    # share one group: apply()'s union origin. The browser still has one
    # word per original word, and each of the three gets its share of the
    # spoken phrase's time.
    # -------------------------------------------------------

    def normalize_spaced_unit_ratios(self, tracker: AlignmentTracker) -> None:

        if self._spaced_unit_ratio_re is None:
            return

        tracker.apply(self._spaced_unit_ratio_re, lambda m: re.sub(r"\s+", "", m.group(0)))

    # -------------------------------------------------------
    # Counter: "Page 3 / 10" -> "Page 3 out of 10"
    #
    # See _NUMERIC_COUNTER_RE. Only the slash is replaced, and it takes the
    # slash's own origin, so both numbers and the word before keep theirs
    # (the same reasoning as _SPACED_SLASH_RE: a whole-match replacement
    # would fold the neighbouring words into the "out of").
    # -------------------------------------------------------

    def normalize_numeric_counters(self, tracker: AlignmentTracker) -> None:

        def _repl(match):
            slash = match.start("slash")
            origin = tracker.origin[slash]
            return [
                (match.start(), slash),
                Verbatim(text="out of", origins=(origin,) * 6),
                (slash + 1, match.end()),
            ]

        tracker.apply_segments(_NUMERIC_COUNTER_RE, _repl)

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

        # Word boundaries must be the browser's, or the highlighter and the
        # timings drift apart -- see normalize_dom_whitespace().
        text = normalize_dom_whitespace(text)

        tracker = AlignmentTracker(text)

        #
        # "&nbsp;" and its variants written as text -> a space, before any
        # other pass can see (and voice) the entity.
        #

        self.decode_nbsp_entities(tracker)

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
        # A minus sign in front of a number ("-50 °C") -> the spoken word
        # "minus". Must run before the dash passes below and before the
        # symbol / unit passes, none of which would voice the sign.
        #

        self.expand_negative_numbers(tracker)

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
        # NBSP and the other Unicode spaces -> ordinary spaces, so Piper is
        # handed clean text. Same width, same origins: no word moves.
        #

        self.normalize_unicode_spaces(tracker)

        #
        # "wheelbase (L)" and "(50L)" -- a bracketed L that must be
        # resolved before the parenthetical pass sees it: "(L)" would be
        # read as the bare letter, and a one-token "(50L)" would be
        # shielded as a part code and never reach the unit rules.
        #

        self.expand_length_symbols(tracker)
        self.expand_bracketed_litres(tracker)

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

        # ==================================================================
        # Slash / unit / single-letter handling happens in THREE passes, in
        # this order. The order is what keeps "L/L" (Latitude/Longitude)
        # apart from "L" (litre or length), and stops "45 kg/hr" being read
        # as "45 kilograms slash Hour".
        #
        #   PASS 1  exact compound tokens   ("L/L", "kg/hr", "g/kW.h",
        #           "L/100km", "L/h") -- matched whole, first of all
        #   PASS 2  single-character / unit rules  ("2.0 L", "50L",
        #           "L = 250", "12°N", "25 mm") -- may only see what
        #           pass 1 left behind
        #   PASS 2b spaced slash  ("Page 3 / 10" -> "Page 3 out of 10",
        #           "SAFE / ARM" -> "SAFE or ARM"; see
        #           normalize_numeric_counters() and
        #           normalize_spaced_slashes()) -- after the compounds and
        #           units, so "kg/hr" and a key such as "TGT / T45" are
        #           already expanded, and before the fallback below. A
        #           spaced unit ratio ("kg / hr") is first joined to
        #           "kg/hr" ahead of PASS 1, which then expands it.
        #   PASS 3  generic slash fallback  ("task/document" -> "task or
        #           document"; see normalize_slashes() further down)
        # ==================================================================

        #
        # PASS 1 -- exact compound tokens.
        #
        # Client-dictionary keys containing "/" or "." (case-sensitive,
        # expansion protected from every later pass), then UNITS' own ratios
        # ("km/h", "kg/min", "°/s": case-insensitive, protected the same
        # way), then the compound litre regexes. All run before
        # replace_units and the "L" rules,
        # which would otherwise take the "kg" of "kg/hr", the "g" of
        # "g/kW.h" or the first "L" of "L/L" and leave the rest to the slash
        # step. Bare unit keys ("MW", "ms", "hr") are NOT here: they stay
        # after the unit pass so that "5 MW" is still megawatts.
        #

        protected_client_dictionary: List[Verbatim] = []

        # A unit ratio written with spaces ("50 km / h", "kg / hr") is joined
        # first, so the compound pass below can expand it as it does "km/h".
        self.normalize_spaced_unit_ratios(tracker)

        self.replace_client_dictionary(tracker, protected_client_dictionary, compound=True)
        self.expand_unit_ratios(tracker, protected_client_dictionary)
        self.expand_litre_compounds(tracker)

        #
        # PASS 2 -- single-character rules and units.
        #
        # "L" after a number is litres (singular/plural); a bearing such as
        # "12°N" is "12 degrees North"; then the unit table ("25mm",
        # "220V"). ("L =" -> "length equals" belongs here too, but needs
        # the dictionary passes to have run first -- see
        # expand_dimension_abbreviations.)
        #

        self.expand_litres(tracker)
        self.expand_compass_degrees(tracker)
        self.replace_units(tracker)

        #
        # Client Dictionary, every other key -- case-SENSITIVE, expansion
        # protected from every pass below (see replace_client_dictionary).
        #

        self.replace_client_dictionary(tracker, protected_client_dictionary, compound=False)

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
        # PASS 2b -- spaced slashes, in this order:
        #   a counter    "Page 3 / 10"                  -> "Page 3 out of 10"
        #   the rest     "Inspection / Maintenance"     -> "Inspection or
        #                Maintenance"
        # (A spaced UNIT ratio, "kg / hr", was joined up before PASS 1.)
        # Unspaced slashes (ratios, identifiers) are left for the pass below.
        #

        self.normalize_numeric_counters(tracker)
        self.normalize_spaced_slashes(tracker)

        #
        # PASS 3 -- generic slash fallback: whatever slash-joined words
        # passes 1 and 2 left alone -> "word or word" (before the symbol
        # pass turns a bare "/" into " slash ")
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
        self.restore_protected_client_dictionary(tracker, protected_client_dictionary)

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
