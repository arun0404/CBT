"""
Alignment tracking between the ORIGINAL text (as authored, containing
abbreviations/units/symbols) and the PROCESSED text (fully expanded,
sent to Piper for synthesis).

Why this exists
----------------
The browser highlights words from the ORIGINAL DOM text. The TTS engine
speaks the PROCESSED (expanded) text. Whenever an expansion changes the
word count ("HAL" -> "Hindustan Aeronautics Limited", "25mm" -> "25
millimetres", "&" -> " and "), a naive word-index-for-word-index mapping
between the two texts drifts out of sync after the first expansion.

AlignmentTracker fixes this by tracking, character-by-character, which
ORIGINAL word every character in the working text descended from, as
each substitution stage (dictionary / units / symbols) is applied. At
the end, it groups processed words back to the original word(s) that
produced them, so a per-original-word timing span can be computed no
matter how many processed words that original word expanded into.
"""

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple, Union


# Tokenization used on the ORIGINAL side. This intentionally matches
# highlighter.js (`textContent.match(/\S+|\s+/g)`), which is what
# actually defines one "word" (and therefore one highlight span) in
# the browser. Do not change this without changing highlighter.js too.
ORIGINAL_WORD_RE = re.compile(r"\S+")

# Tokenization used on the PROCESSED side, for the purpose of counting
# words to compute timings. This matches TimingGenerator.split_words()
# in timing.py. Keeping both in one place avoids the two ever drifting
# apart silently.
PROCESSED_WORD_RE = re.compile(r"\b[\w'-]+\b")


@dataclass(frozen=True)
class Verbatim:
    """
    An explicit (text, per-character origin) pair used as a replacement
    segment (see AlignmentTracker.apply_segments) when the correct origin
    for some text can't be sliced from the tracker's CURRENT text/origin
    arrays -- because it was captured at an earlier point in the
    pipeline and the text around it has since been rewritten by later
    passes (dictionary/units/symbols), invalidating any positions taken
    at that earlier point.

    `origins` must have exactly one entry per character of `text`.
    """

    text: str
    origins: Tuple[Tuple[int, ...], ...]


# One replacement segment, as returned by an apply_segments() repl_fn:
#   - str            : brand-new text, tagged with the union origin of
#                       the whole match (same rule apply() uses for its
#                       entire replacement) -- for punctuation/decoration
#                       that isn't really "spoken content" of any one
#                       original word.
#   - (start, end)    : a verbatim slice of the CURRENT self.text /
#                       self.origin (absolute positions), preserving
#                       that slice's real per-character origin instead
#                       of collapsing it -- for content carried through
#                       a substitution unmodified.
#   - Verbatim        : explicit text with an explicit per-character
#                       origin array, for content whose true origin was
#                       captured earlier and can no longer be sliced
#                       from the current text (see class docstring).
Segment = Union[str, Tuple[int, int], Verbatim]


@dataclass
class WordGroup:
    """
    One contiguous run of original words that were transformed together,
    and the range of processed words their transformation produced.
    """

    original_indices: List[int]
    processed_start: int   # inclusive
    processed_end: int     # exclusive


@dataclass
class AlignmentResult:
    processed_text: str
    original_words: List[str]
    processed_words: List[str]
    groups: List[WordGroup]


class AlignmentTracker:
    """
    Wraps the existing regex-substitution pipeline (dictionary lookups,
    unit expansion, symbol expansion) and tracks word-origin provenance
    through each pass, without changing any of the matching logic or
    replacement values already defined in symbols.py / units.py / etc.
    """

    def __init__(self, original_text: str):
        self.original_text = original_text
        self.original_words = ORIGINAL_WORD_RE.findall(original_text)

        # origin[i] = tuple of original-word indices that character i
        # of the CURRENT working text descends from.
        self.text = original_text
        self.origin: List[Tuple[int, ...]] = self._seed_origin(original_text)

    # ------------------------------------------------------------------
    def _seed_origin(self, text: str) -> List[Tuple[int, ...]]:

        origin: List[Optional[Tuple[int, ...]]] = [None] * len(text)

        for word_index, match in enumerate(ORIGINAL_WORD_RE.finditer(text)):
            start, end = match.span()
            for i in range(start, end):
                origin[i] = (word_index,)

        # Whitespace inherits the origin of the preceding word so that
        # a match spanning "word + trailing/leading space" still
        # resolves to a sensible origin. Leading whitespace (before any
        # word) inherits the first word's origin.
        first_origin = (0,) if self.original_words else ()
        running = first_origin

        for i in range(len(text)):
            if origin[i] is not None:
                running = origin[i]
            else:
                origin[i] = running

        return origin  # type: ignore[return-value]

    # ------------------------------------------------------------------
    def apply(self, pattern: re.Pattern, repl_fn: Callable[[re.Match], str]) -> None:
        """
        Runs one substitution pass (equivalent to re.sub(pattern, repl_fn,
        self.text)) while keeping the origin array in sync with the
        resulting text.
        """

        pieces: List[str] = []
        new_origin: List[Tuple[int, ...]] = []
        last_end = 0

        for match in pattern.finditer(self.text):
            start, end = match.span()

            if start < last_end:
                # Overlaps a match already consumed earlier in this pass.
                continue

            # Untouched text before this match.
            pieces.append(self.text[last_end:start])
            new_origin.extend(self.origin[last_end:start])

            replacement = repl_fn(match)

            matched_origins = set()
            for i in range(start, end):
                matched_origins.update(self.origin[i])
            group_origin = tuple(sorted(matched_origins))

            pieces.append(replacement)
            new_origin.extend([group_origin] * len(replacement))

            last_end = end

        pieces.append(self.text[last_end:])
        new_origin.extend(self.origin[last_end:])

        self.text = "".join(pieces)
        self.origin = new_origin

    # ------------------------------------------------------------------
    def apply_segments(self, pattern: re.Pattern, repl_fn: Callable[[re.Match], List["Segment"]]) -> None:
        """
        Like apply(), but repl_fn returns a LIST of segments (see the
        Segment type above) instead of a flat string.

        apply() tags an ENTIRE replacement with the union origin of
        everything the match consumed -- correct when a substitution
        genuinely collapses several original words into fewer/different
        ones (e.g. "Auxiliary Power Unit" -> "APU"), but wrong when a
        transformation only touches a match's BOUNDARY (e.g. dropping
        the parentheses around "(Refer to Task 79-24-00-870-7A)" while
        leaving the words inside untouched): unioning would smear four
        distinct original words into one indistinguishable timing group,
        so the browser highlight for each word inside the parentheses
        can no longer track its own forced-alignment timing -- see
        replace_parentheticals() in preprocess.py, which is why this
        method exists.
        """

        pieces: List[str] = []
        new_origin: List[Tuple[int, ...]] = []
        last_end = 0

        for match in pattern.finditer(self.text):
            start, end = match.span()

            if start < last_end:
                continue

            pieces.append(self.text[last_end:start])
            new_origin.extend(self.origin[last_end:start])

            matched_origins = set()
            for i in range(start, end):
                matched_origins.update(self.origin[i])
            group_origin = tuple(sorted(matched_origins))

            for segment in repl_fn(match):
                if isinstance(segment, Verbatim):
                    pieces.append(segment.text)
                    new_origin.extend(segment.origins)
                elif isinstance(segment, str):
                    pieces.append(segment)
                    new_origin.extend([group_origin] * len(segment))
                else:
                    seg_start, seg_end = segment
                    pieces.append(self.text[seg_start:seg_end])
                    new_origin.extend(self.origin[seg_start:seg_end])

            last_end = end

        pieces.append(self.text[last_end:])
        new_origin.extend(self.origin[last_end:])

        self.text = "".join(pieces)
        self.origin = new_origin

    # ------------------------------------------------------------------
    def replace_literal(self, literal: str, replacement: str) -> None:
        """Convenience wrapper for the plain text.replace() style substitutions
        used by symbol expansion (no regex needed, but must still track origin)."""

        if literal not in self.text:
            return

        pattern = re.compile(re.escape(literal))
        self.apply(pattern, lambda m: replacement)

    # ------------------------------------------------------------------
    def finalize(self, cleanup_fn: Optional[Callable[[str], str]] = None) -> AlignmentResult:
        """
        Computes the final original-word -> processed-word grouping.

        `cleanup_fn`, if given, is applied to the finished text (e.g. to
        collapse repeated whitespace) AFTER grouping is computed. Cleanup
        only touches whitespace, never word tokens, so it cannot change
        the grouping and is safe to apply afterward.
        """

        processed_words: List[str] = []
        word_origins: List[Tuple[int, ...]] = []

        for match in PROCESSED_WORD_RE.finditer(self.text):
            start, end = match.span()
            processed_words.append(match.group())

            origins = set()
            for i in range(start, end):
                origins.update(self.origin[i])
            word_origins.append(tuple(sorted(origins)))

        groups = self._build_groups(processed_words, word_origins)

        processed_text = cleanup_fn(self.text) if cleanup_fn else self.text

        return AlignmentResult(
            processed_text=processed_text,
            original_words=self.original_words,
            processed_words=processed_words,
            groups=groups,
        )

    # ------------------------------------------------------------------
    def _build_groups(
        self,
        processed_words: List[str],
        word_origins: List[Tuple[int, ...]],
    ) -> List[WordGroup]:

        n_original = len(self.original_words)
        n_processed = len(processed_words)

        groups: List[WordGroup] = []

        next_original = 0
        proc_i = 0

        while next_original < n_original:

            if proc_i >= n_processed:
                # No processed words left to account for the remaining
                # original words: they produced no spoken output at all —
                # trailing punctuation that stands as its own \S+ token
                # (". )" ").)" ")" after a code), or a trailing word fully
                # deleted by a substitution. Give EACH its own degenerate
                # (zero-width) group at the very end, rather than one
                # group spanning them all — so downstream timing keeps
                # every one of them a distinct zero-width, non-target
                # marker that cannot absorb the tail of the last spoken
                # word's highlight window (which is what made the
                # highlight jump off "A" onto a trailing "." or ").").
                for trailing_index in range(next_original, n_original):
                    groups.append(WordGroup(
                        original_indices=[trailing_index],
                        processed_start=n_processed,
                        processed_end=n_processed,
                    ))
                break

            origins = word_origins[proc_i] or (next_original,)

            # Original words that come BEFORE the first one this next
            # processed word descends from produced no spoken output at
            # all -- e.g. a leading emoji / invisible character that was
            # stripped before synthesis (see
            # TextPreprocessor.strip_tts_only_characters), or a token
            # deleted outright by a substitution.
            #
            # Folding them into the following spoken word's group (the
            # old behaviour) meant that group then had to spread its one
            # real, audio-derived timing span across them in
            # generate_from_word_bounds() -- which is what made a leading
            # 📄 hold the highlight while the audio was already several
            # words into the sentence, then "jump" onto the real word.
            # Give each stripped word its own zero-width group anchored
            # at this position instead, exactly as the trailing-word
            # branch above already does. Timing entry count (and so DOM
            # parity) is unchanged: every original index still lands in
            # exactly one group.
            first_claimed = min(origins)

            if first_claimed > next_original:
                for silent_index in range(next_original, first_claimed):
                    groups.append(WordGroup(
                        original_indices=[silent_index],
                        processed_start=proc_i,
                        processed_end=proc_i,
                    ))
                next_original = first_claimed

            group_end_original = max(max(origins), next_original)

            proc_start = proc_i
            proc_i += 1

            # Absorb any further processed words that still belong to
            # this same original-word range (this is what lets one
            # original word expand into many processed words).
            while proc_i < n_processed:
                nxt = word_origins[proc_i] or ()
                if nxt and min(nxt) <= group_end_original:
                    group_end_original = max(group_end_original, max(nxt))
                    proc_i += 1
                else:
                    break

            original_indices = list(range(next_original, group_end_original + 1))

            groups.append(WordGroup(
                original_indices=original_indices,
                processed_start=proc_start,
                processed_end=proc_i,
            ))

            next_original = group_end_original + 1

        return groups