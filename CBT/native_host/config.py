import hashlib
from pathlib import Path

# --------------------------------------------------
# Project Root
# --------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------
# HTML
# --------------------------------------------------

HTML_DIR = ROOT / "html"
JS_DIR = HTML_DIR / "js"
CSS_DIR = HTML_DIR / "css"

# --------------------------------------------------
# Piper
# --------------------------------------------------

PIPER_DIR = ROOT / "piper"

PIPER_EXE = PIPER_DIR / "piper.exe"

ESPEAK_DATA = PIPER_DIR / "espeak-ng-data"

# Seconds of silence Piper renders into the WAV itself after each
# sentence-ending clause (a period/!/?/: — piper.exe's own
# --sentence_silence flag; espeak-ng's phonemizer is what actually
# detects the clause break from punctuation). Not passing this flag at
# all leaves Piper's own undocumented-here default in effect, which is
# short enough that a period inserted purely to create a pause (e.g.
# TextPreprocessor.insert_heading_pauses, for the heading-pause feature)
# produces no perceptible gap in the audio.
#
# This is a synthesis-time, audio-level setting — it has nothing to do
# with timing.py/forced_aligner.py, which only ever measure whatever
# silence genuinely exists in the rendered WAV; they cannot manufacture
# a pause that was never actually synthesized.
SENTENCE_SILENCE_SECONDS = 0.5

# --------------------------------------------------
# Voices
# --------------------------------------------------

VOICE_DIR = ROOT / "voices"

MALE_MODEL = VOICE_DIR / "male.onnx"

FEMALE_MODEL = VOICE_DIR / "female.onnx"


def _fingerprint_files(*paths: Path) -> str:
    """
    Cheap proxy for "have any of these files changed" — hashes each
    path's name + size + mtime rather than its full content, so it
    stays fast even for large voice model files and can safely run on
    every process startup.
    """

    h = hashlib.sha256()

    for path in paths:
        try:
            stat = path.stat()
            h.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
        except OSError:
            h.update(f"{path.name}:missing".encode("utf-8"))

    return h.hexdigest()[:16]


# Folded into the alignment-cache key (see cache.py / speech_pipeline.py)
# so that swapping out piper.exe or either voice model invalidates any
# audio cached under the old files, even though the "voice" string
# itself (male/female) didn't change.
VOICE_FINGERPRINT = _fingerprint_files(PIPER_EXE, MALE_MODEL, FEMALE_MODEL)

# --------------------------------------------------
# Output
# --------------------------------------------------

OUTPUT_DIR = ROOT / "output"

# Per-generation scratch WAV files. Every synthesis call writes to a
# freshly-named file here (see speech_pipeline.py) so that concurrent
# generations — e.g. the requested voice and the background prefetch
# of the other voice — never collide, and each scratch file is deleted
# immediately after it's been copied into the cache (or moved into the
# ephemeral directory below).
SCRATCH_DIR = OUTPUT_DIR / "_scratch"

# Holds audio+timings for the rare case where forced alignment fell
# back to naive equal-duration timing — see cache.py's "never cache
# fallback" rule. These are request-scoped, not reused across
# requests, so this directory is kept small via LRU trimming
# (EPHEMERAL_MAX_ENTRIES) instead of growing unbounded.
EPHEMERAL_DIR = OUTPUT_DIR / "ephemeral"
EPHEMERAL_MAX_ENTRIES = 20

# --------------------------------------------------
# Alignment / Timing
# --------------------------------------------------

# Floor on how short a single word's highlight window is allowed to
# be, in seconds. Forced-alignment durations for short function words
# ("to", "is", "at") are usually accurate but sometimes only 1-2
# frames wide (~20-40ms) — long enough to be correct, but short enough
# to be visually imperceptible even with perfect sampling. This nudges
# such words up to a readable minimum without materially distorting
# the rest of the timeline (see timing.py: enforce_minimum_duration).
MIN_WORD_HIGHLIGHT_DURATION = 0.08

# --------------------------------------------------
# Alignment Cache
#
# A cache entry stores a COMPLETE speech bundle — audio (.wav) and
# per-original-word timings (.json) — keyed by everything that could
# change the correct output. A cache hit means nothing is regenerated
# at all, which is what makes instant voice-switching possible; see
# speech_pipeline.py.
# --------------------------------------------------

ALIGNMENT_CACHE_DIR = OUTPUT_DIR / "alignment_cache"

# How many entries to keep in the fast in-memory tier before evicting
# the least-recently-used one. Each entry is a small object (a list of
# timing dicts + a Path — the audio bytes themselves stay on disk), so
# this can be generous without meaningful memory cost.
ALIGNMENT_CACHE_MAX_MEMORY_ENTRIES = 256

# Both directories above live under OUTPUT_DIR, which host.py's generic
# /audio/<path:filename> route already serves in full — so cached and
# ephemeral files are reachable with no additional Flask routes.
ALIGNMENT_CACHE_URL_PREFIX = "/audio/" + ALIGNMENT_CACHE_DIR.relative_to(OUTPUT_DIR).as_posix()
EPHEMERAL_URL_PREFIX = "/audio/" + EPHEMERAL_DIR.relative_to(OUTPUT_DIR).as_posix()

# Bump this whenever the alignment/timing ALGORITHM, or the cache
# entry FORMAT, changes in a way that should invalidate previously
# cached results, even though the input text/voice/speed didn't
# change.
#   v1 -> v2: cache entries now store audio alongside timings (v1
#             entries only had timings and are no longer usable).
#   v2 -> v3: emoji/invisible characters are now stripped before
#             synthesis (changes the audio itself for any text
#             containing them) and each timing entry gained an
#             "emoji" field (see timing.py / text_sanitizer.py). Both
#             mean a v2 cache entry is no longer a correct response.
#   v3 -> v4: fixed a highlight-drift bug where every word inside a
#             multi-word parenthetical -- "(Refer to Task
#             79-24-00-870-7A)", "(after we checked the fuel levels)" --
#             was folded into a single WordGroup, so its whole span got
#             one evenly-divided highlight timing instead of each word
#             keeping its own real forced-alignment bounds (see
#             AlignmentTracker.apply_segments / replace_parentheticals
#             in text_processing/alignment.py and preprocess.py). Also
#             fixed the CODE/INITIALISM placeholder sentinel (\x1E,
#             previously) being treated as whitespace by \s in later
#             regex passes -- e.g. replace_units' "\d+\s*unit" pattern --
#             which could delete the placeholder's closing delimiter and
#             corrupt adjacent text. v3 timings for any text containing
#             a multi-word parenthetical are no longer correct.
#   v4 -> v5: fixed enforce_minimum_duration()'s overrun correction: it
#             used to rescale EVERY word's start/end proportionally
#             whenever floor-padding pushed the last word past the
#             audio's duration, which shifts even a word whose timing
#             was already correct -- and the shift grows with elapsed
#             time, so it reads as the highlight progressively outrunning
#             the audio. Now only the trailing run of entries that
#             actually inherited padding debt is compressed; everything
#             before it keeps its exact original timing (see
#             TimingGenerator._compress_trailing_debt in timing.py). v4
#             timings for any text where this ever triggered are no
#             longer correct.
#   v5 -> v6: multi-segment hyphenated reference/task codes (e.g.
#             "79-24-00-870-7A") are now placeholder-protected from
#             replace_units -- previously a trailing "<digit><letter>"
#             tail could be misread as a unit ("7A" -> "7 amperes"),
#             corrupting and mispronouncing the code (see
#             protect_reference_codes / REFERENCE_CODE_RE in
#             preprocess.py). Changes the actual synthesized audio for
#             any text containing such a code, so v5 entries for it are
#             no longer correct.
#   v6 -> v7: fixed ForcedAligner._normalize_word (aligner/
#             forced_aligner.py) silently deleting every digit in a
#             token that mixes digits with other characters -- e.g. the
#             protected reference code "79-24-00-870-7A" collapsed to a
#             single alignable letter ("a"), so MMS_FA had nothing to
#             anchor the word's true START to and placed its whole
#             timing window near the very END of the code instead
#             (highlight stuck on the previous word, then jumping to
#             cover the whole code at once). Digit runs embedded in a
#             non-purely-numeric token are now spelled out individually
#             so the aligner sees the code's full spoken content. Changes
#             computed timings for any text containing such a token, so
#             v6 entries for it are no longer correct.
#   v7 -> v8: multi-segment hyphenated reference/task codes (e.g.
#             "79-24-00-870-7A") are no longer sent to Piper verbatim --
#             they're rewritten to their natural spoken form ("seventy-
#             nine, twenty-four, zero-zero, eight-seventy, seven-a")
#             before synthesis, since Piper's phonemizer read a bare
#             "-" as the word "dash" and mangled a doubled "00" (see
#             _speak_reference_code / protect_reference_codes in
#             preprocess.py). Changes the actual synthesized audio for
#             any text containing such a code, so v7 entries for it are
#             no longer correct.
#   v8 -> v9: an all-zero digit run inside a reference/task code now
#             reads by its conventional spoken name -- "00" -> "double
#             zero", "000" -> "triple zero" -- instead of being spelled
#             digit-by-digit ("zero-zero") (see _ZERO_RUN_WORDS /
#             _speak_digit_run in preprocess.py). Changes the actual
#             synthesized audio for any text containing such a code, so
#             v8 entries for it are no longer correct.
#   v9 -> v10: uppercase sequences containing 4+ consecutive consonants
#              (e.g. "ALHMKMTM") are now expanded to letter-by-letter
#              form ("A L H M K M T M") before synthesis, since Piper
#              mispronounces such sequences as a single garbled word (see
#              expand_initialisms / _is_unpronounceable in
#              text_processing/preprocess.py). Changes the synthesized
#              audio for any text containing a qualifying sequence, so
#              v9 entries for it are no longer correct.
#   v10 -> v11: the initialism-expansion pass no longer misfires on real
#               words that are A/E/I/O/U-free-looking only because Y is
#               counted as a consonant ("SYSTEM" -> "S Y S T E M",
#               "SCHWA", etc.) -- expansion now also requires the word to
#               contain no true vowel (see _is_unpronounceable). It also
#               pads its spelled output, and reference/task codes now
#               absorb a leading glue hyphen and pad their placeholder,
#               so a code written glued to an alphabetic prefix
#               ("ALHMKMTM-SYSTEM-79-24-00-00-920A-A") no longer produces
#               fused, un-alignable tokens ("M-SYSTEM",
#               "SYSTEMseventy-nine") that drift the highlight. Changes
#               synthesized audio and computed timings for any text with
#               such a sequence, so v10 entries for it are no longer
#               correct.
#   v11 -> v12: a forward slash directly between two unspaced words
#               ("task/document", "inspection/check") is now spoken as
#               "or" ("task or document") instead of the bare word
#               "slash" (see normalize_slashes / _WORD_SLASH_WORD_RE in
#               preprocess.py). File paths, URLs and reference-code tails
#               are unaffected. Changes synthesized audio and computed
#               timings for any text containing such a slash, so v11
#               entries for it are no longer correct.
#   v12 -> v13: a non-spoken leading/interior original word (a stripped
#               emoji or invisible character, or a token deleted by a
#               substitution) now gets its OWN zero-width alignment group
#               instead of being folded into the following spoken word's
#               group (see AlignmentTracker._build_groups). Previously
#               that spoken word had to spread its real audio span across
#               the stripped word too, which held the highlight on a
#               leading "📄" while the audio was already mid-sentence and
#               then jumped it onto the word. strip_tts_only_characters
#               also now leaves a single space (not "") where it removes
#               a run that isn't wedged between two word characters, so
#               the origin map keeps a slot for the removed symbol and
#               the words around it can't fuse. Changes computed emoji
#               timing entries (imperceptibly -- they remain non-spoken)
#               for any text containing a stripped symbol, so v12 entries
#               for it are no longer identical.
#   v13 -> v14: a lone punctuation/delimiter token that stands as its own
#               \S+ word (". )" ").)" ")" left after a task code, a
#               stray "--") is now flagged non-spoken (zero-width,
#               non-target) in the timing bundle, exactly like an emoji.
#               Each trailing such token also gets its own degenerate
#               alignment group (AlignmentTracker._build_groups) instead
#               of sharing one. Previously the trailing "." received a
#               real window at end-of-audio and the highlight jumped off
#               the final spoken letter ("A") onto it. A non-alnum token
#               that DID produce speech ("&" -> "and") is unaffected.
#               Changes the trailing/again-non-spoken timing entries for
#               any text with such a token, so v13 entries for it are no
#               longer identical.
#   v14 -> v15: replace_units now runs BEFORE the dictionary passes, and
#               the bare single-letter dimension keys ("L"/"W"/"H") were
#               removed from ENGINEERING_TERMS -- they were substituted
#               standalone and corrupted unit compounds ("km/h" -> "km
#               slash Height") and prose. Dimension abbreviations are now
#               expanded only inside an explicit "L x W x H" / "H:"
#               pattern (expand_dimension_abbreviations). The slash pass
#               also handles multi-slash runs ("L/W/H" -> "L or W or H")
#               and bare unit compounds ("km/h" -> "kilometres per
#               hour"). The dictionary_fingerprint already busts on the
#               ENGINEERING_TERMS edit; this bump covers the pass
#               reordering, which no fingerprint sees. Changes audio and
#               timings for any text with a unit, dimension letter, or
#               slash run, so v14 entries for it are no longer correct.
#   v15 -> v16: hyphenated reference/task codes are now read STRICTLY
#               digit-by-digit ("79-24-00-870-7A" -> "seven nine, two
#               four, zero zero, eight seven zero, seven a") instead of
#               as cardinal numbers ("seventy-nine, twenty-four, double
#               zero, eight-seventy, seven-a") -- see _speak_digit_run in
#               preprocess.py. These identifiers are opaque field
#               sequences, not quantities, and an aggregated reading
#               ("eight-seventy") loses the digit sequence a technician
#               needs to look the workcard up. No fingerprint covers this:
#               dictionary_fingerprint only hashes the dictionaries, and
#               this is a code change in the rendering function. Changes
#               the audio AND the processed-word count (and therefore the
#               timings) for any text containing such a code, so v15
#               entries for it are no longer correct.
#   v16 -> v17: the trailing letters of a reference code are now spoken
#               UPPERCASE ("79-24-00-870-7A" -> "... seven A", not
#               "... seven a"). A standalone lowercase "a" is the English
#               article and espeak-ng voices it as a reduced schwa
#               ("uh"), so every workcard number ended in a mumbled
#               "seven uh"; an isolated capital is read as the letter
#               name, matching how expand_initialisms already spells out
#               unpronounceable runs. Digit words were also pinned to an
#               explicit table (_DIGIT_WORDS) instead of num2words —
#               identical output, so that part alone would not have
#               needed a bump. Changes the audio for any text containing
#               a lettered reference code, so v16 entries for it are no
#               longer correct.
#   v17 -> v18: a STANDALONE dash (whitespace on both sides) is now
#               rewritten to a comma pause instead of reaching Piper
#               intact, which read it aloud as the word "dash"
#               ("Mounting Bracket - Driveshaft" -> "Mounting Bracket
#               DASH Driveshaft"). Scoped to whitespace-bounded dashes
#               only, so compound words ("Anti-Lock"), hyphenated
#               numbers, unit ranges ("5mm-10mm") and reference codes are
#               untouched -- see normalize_standalone_dashes. Changes the
#               audio and the processed-word count for any text
#               containing such a dash, so v17 entries for it are no
#               longer correct.
#   v18 -> v19: reference codes are now spoken as ONE continuous run of
#               space-separated digit words with no internal punctuation
#               ("seven nine two four zero zero eight seven zero seven
#               A") instead of a comma per segment boundary ("seven
#               nine, two four, ..."). With --sentence_silence in force
#               each of those commas asked the phonemizer for a clause
#               break mid-code, and a break right after "zero" clipped
#               its trailing vowel into an audible artefact. Changes the
#               audio for any text containing a reference code, so v18
#               entries for it are no longer correct.
#   v19 -> v20: a 0 inside a reference code is now spoken "oh" rather
#               than "zero" (_ZERO_WORD in preprocess.py) -- the ordinary
#               convention for reading an identifier aloud, one syllable
#               instead of two, and it drops the /z-i-r-oʊ/ cluster that
#               was reportedly clipping mid-code. Affects reference codes
#               only (_DIGIT_WORDS is reachable only from
#               _speak_reference_code); prose numbers are untouched.
#               Changes the audio for any text containing a code with a
#               0 in it, so v19 entries for it are no longer correct.
#   v20 -> v21: "oh" reverted to "zero" (_ZERO_WORD), and a run of the
#               SAME digit inside a code is now collapsed into a counted
#               phrase -- "00" -> "double zero", "000" -> "triple zero"
#               (_REPEAT_PREFIXES / _chunk_repeat_run). Attention-based
#               neural TTS under-articulates a token repeated
#               immediately after itself, which is exactly what "zero
#               zero" was; the counted form removes the repetition so
#               there is nothing to collapse, needs no punctuation, and
#               therefore cannot interact with
#               SENTENCE_SILENCE_SECONDS. Changes the audio for any text
#               containing a code with a repeated digit, so v20 entries
#               for it are no longer correct.
#   v21 -> v22: client_dictionary.json is now matched CASE-SENSITIVELY
#               (TextPreprocessor.replace_client_dictionary: one compiled
#               pattern, longest key first, no re.IGNORECASE) so entries
#               that differ only in case stay distinct -- "CBS" Chip
#               Burning System vs "CBs" Circuit Breakers, "MFDS" Main
#               Flexible Drive Shaft vs "MFDs" Multi Function Displays,
#               all four newly added. The dictionary_fingerprint busts on
#               the new entries; this bump covers the matching change
#               itself, which no fingerprint sees: a key no longer fires
#               on a different-case spelling, so e.g. the ordinary word
#               "ram" is no longer rewritten to "Random Access Memory".
#               Changes the audio and processed-word count for any text
#               containing a client-dictionary key in a different case, so
#               v21 entries for it are no longer correct.
#               ENGINEERING_TERMS and ABBREVIATIONS are still
#               case-insensitive (so "cam" is still caught by "CAM" there).
#   v22 -> v23: the ambiguous letter "L"/"l" is now resolved by context
#               (TextPreprocessor.expand_litres / expand_bracketed_litres /
#               expand_length_symbols / the L-assignment rule in
#               expand_dimension_abbreviations). Litres: "1 L" is now
#               singular ("1 litre", was "1 litres"), "2.0 L engine" is a
#               singular modifier, "(50L)" is expanded instead of being
#               shielded as a part code, and "L/100km" is "litres per
#               hundred kilometres" (was "litres or 100 kilometres").
#               Length: "l x w x h" in lower case, "L = 250" ("length
#               equals 250") and "wheelbase (L)" / "overall length (L)"
#               are expanded instead of reading the bare letter. No
#               dictionary changed, so dictionary_fingerprint does NOT
#               move; this bump is what invalidates v22 entries for any
#               text containing one of those shapes. Changes the audio and
#               the processed-word count for them, so those entries are no
#               longer correct.
#   v23 -> v24: client_dictionary.json expansions are now FINAL text:
#               replace_client_dictionary() parks each one behind a
#               \x02CD<n>\x02 placeholder while the later passes run and
#               restores it afterwards, so engineering.py / abbreviations.py
#               / the slash pass can no longer rewrite it ("Turn
#               Co-ordination" was spoken "Turn Company-ordination", "No
#               Picture In Picture" "Number Picture In Picture"). "&" and
#               "+" inside an expansion are still voiced "and" / "plus";
#               anything else (a slash, "RPM") must now be written out in
#               the entry. Also: "L/min", "L/h" and "L/hr" are now "litres
#               per minute/hour" (they were "litres or h"; the unit part is
#               lower-case only, so "L/H" for Left Hand is untouched). The
#               dictionary content changed too (so the fingerprint moves),
#               but this bump covers the logic change, which no fingerprint
#               sees. Changes the audio for any text that hits a client
#               entry containing a slash, "No", "Co", "ID" etc., and for
#               litre rates, so v23 entries for it are no longer correct.
#   v24 -> v25: slash/unit/single-letter handling now runs in three ordered
#               passes (see process_with_alignment): (1) exact COMPOUND
#               tokens first -- client-dictionary keys containing "/" or
#               "." ("L/L", "kg/hr", "g/kW.h", "N.m") and the litre
#               compounds ("L/100km", "L/h"); (2) single-character / unit
#               rules ("2.0 L", "L =", "25 mm"); (3) the generic slash
#               fallback. Before, the unit pass ran first and took the
#               "kg" of "45 kg/hr" and the "g" of "210 g/kW.h", leaving
#               "45 kilograms slash Hour" / "210 grams slash kilowatts.h";
#               "30 kg/h" and "210 g/kWh" came out as "kilograms or h" /
#               "grams or kWh". Also new: a bearing such as "12°N" is spoken
#               "12 degrees North" (was "12 degrees N"). Bare unit keys
#               ("MW", "ms", "hr") still run AFTER the unit pass so "5 MW" is
#               megawatts. Changes the audio for any text containing a
#               compound unit or a bearing, so v24 entries for it are no
#               longer correct.
#   v25 -> v26: ALL-CAPS acronyms inside brackets are now read LETTER BY
#               LETTER ("Integrated Air Defence System (IADS)" -> "... I A D
#               S", "(NATO)" -> "N A T O"); before, Piper said them as one
#               word ("eye-ads", "nay-toe"). Decided by spell_spans() in
#               parentheticals.py and applied in replace_parentheticals():
#               a bracketed initialism of the words before it is always
#               spelled; other ALL-CAPS words of 2-6 letters are spelled
#               unless they are a known abbreviation or unit (FIG, KG), an
#               ordinary word (SPEAK_AS_WORD: NOTE, ON, OFF ...), part of a
#               multi-word dictionary key ("AIR COND"), or - in a prose
#               bracket - something the dictionaries expand. Words outside
#               brackets are untouched. Also fixed _is_initialism_of(), which
#               matched greedily and so missed acronyms whose expansion has an
#               "and" ("Integrated Architecture and Display System" -> IADS).
#               PARENTHETICAL_CONFIG_VERSION is now "v2" and the word list is
#               part of dictionary_fingerprint. Changes the audio and the
#               processed-word count for any text containing a bracketed
#               acronym, so v25 entries for it are no longer correct.
#   v26 -> v27: non-spoken structural symbols (dashes, quotes, NBSP,
#               zero-width characters, document emoji) are now handled so
#               that Piper is given clean text AND the browser's word list
#               and the timings cannot drift apart. Measured against the
#               real voices: none of these is spoken (the 📄 emoji IS, and
#               was already stripped), so nothing changes audibly except
#               where noted. (1) Curly double quotes are removed like
#               straight ones (symbols.py); NBSP and the other Unicode
#               spaces become plain spaces; a dash at the very start or end
#               of the text is dropped instead of sent. (2) The backend now
#               splits words where the browser's /\S+/ does: U+FEFF is a
#               word boundary (it fused two words into one before, so every
#               later highlight was off by one), and U+001C-U+001E / U+0085
#               no longer split a word. (3) timing.py flags a word that is
#               only dashes/quotes/invisible characters/emoji silent PER
#               WORD (text_sanitizer.is_non_spoken_word), not only when its
#               whole group produced nothing. Changes the text sent to Piper,
#               and the processed-word count for text containing U+FEFF or
#               those control characters, so v26 entries for such text are no
#               longer correct.
#   v27 -> v28: Roman-numeral list markers are now recognised in all three
#               written forms and in upper case (expand_roman_numeral_list_
#               markers). Before, only a lower-case "i." / "i)" at the start
#               of a line was spoken as a number; "(i)" came out as the letter
#               (", i,"), "II." / "(III)" were left alone, and an enumeration
#               inside a sentence ("(i) clean it; (ii) replace it") was read as
#               letters. Now "i." "i)" "(i)" are "One." ... "Ten." at the start
#               of a line (up to "xx"); multi-letter upper case ("II." "IV)"
#               "(III)") counts too, a single upper-case "I" / "V" / "X" only
#               as "I)" or "(V)" (as "I." / "V." it is a pronoun or an
#               initial); inside a sentence a bracketed numeral is spoken as
#               a number when it starts a clause or belongs to a run, with a
#               pause after a label ("One, clean it; Two, replace it") and
#               none after a reference ("Part (IV)" -> "Part Four"). A bare
#               i, v or x, and "velocity (v)" / "Voltage (V)" / "f(x)", stay
#               letters. Bracketed Roman numerals are no longer spelled as
#               acronyms ("(IV)" was "I V"). Changes the audio for any text
#               containing such a marker, so v27 entries for it are no longer
#               correct.
ALIGNMENT_CACHE_VERSION = "v28"

# How long a request will wait for another in-flight request that's
# already generating the exact same (text, voice, speed, ...) before
# giving up and generating independently. See speech_pipeline.py.
GENERATION_WAIT_TIMEOUT_SECONDS = 120
