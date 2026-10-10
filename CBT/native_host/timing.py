import json
import re
from pathlib import Path

from text_processing.alignment import AlignmentResult
from text_processing.text_sanitizer import is_non_spoken_word
from config import MIN_WORD_HIGHLIGHT_DURATION


class TimingGenerator:

    def __init__(self):
        pass

    def split_words(self, text):

        words = re.findall(r"\b[\w'-]+\b", text)

        return words

    def generate(self, text, duration):
        """
        Naive equal-duration-per-word split over PROCESSED text.
        Kept for backward compatibility / debugging, but no longer used
        directly by host.py — see generate_from_alignment().
        """

        words = self.split_words(text)

        if len(words) == 0:
            return []

        seconds_per_word = duration / len(words)

        current = 0.0

        timings = []

        for word in words:

            start = current

            end = start + seconds_per_word

            timings.append({

                "word": word,

                "start": round(start, 3),

                "end": round(end, 3)

            })

            current = end

        return timings

    # ------------------------------------------------------------------
    # Alignment-aware generation
    #
    # Produces exactly one timing entry per ORIGINAL word (the words the
    # browser actually highlights), even though the audio was synthesized
    # from the fully-expanded PROCESSED text. Each original word's timing
    # spans the full duration of everything its expansion produced.
    # ------------------------------------------------------------------

    def generate_from_alignment(self, alignment: AlignmentResult, duration: float):

        processed_words = alignment.processed_words
        n_processed = len(processed_words)

        if n_processed == 0 or len(alignment.original_words) == 0:
            return []

        # Step 1: naive equal-duration split over the PROCESSED words —
        # this is the actual "how long is each spoken word" estimate.
        # (Swap this block out for real forced-alignment timings later
        # without touching anything below — see generate_from_word_bounds.)
        seconds_per_processed_word = duration / n_processed

        processed_bounds = []
        current = 0.0
        for _ in processed_words:
            start = current
            end = start + seconds_per_processed_word
            processed_bounds.append((start, end))
            current = end

        return self.generate_from_word_bounds(alignment, processed_bounds, duration)

    def generate_from_word_bounds(self, alignment: AlignmentResult, processed_bounds, duration=None):
        """
        Given a (start, end) time bound for every PROCESSED word — whether
        from the naive equal split above or from a real forced-alignment
        model — collapses them into one timing entry per ORIGINAL word
        using the alignment groups.

        Each entry also carries an "emoji" flag (see
        text_sanitizer.is_pure_emoji_word) — True for an original word
        that is entirely emoji/invisible characters and therefore was
        never actually spoken (it was stripped before Piper synthesis —
        see preprocess.py). The frontend uses this to hold the highlight
        on such words for a fixed, readable dwell instead of the
        near-zero-width slot they'd otherwise get purely from audio
        duration — see highlighter.js.

        If `duration` (total audio length in seconds) is given, a final
        pass enforces a minimum highlight duration per word — see
        enforce_minimum_duration() — so words don't stay skippable.
        """

        timings = []
        last_end = 0.0

        for group in alignment.groups:

            degenerate = group.processed_start >= group.processed_end

            if not degenerate:
                group_start = processed_bounds[group.processed_start][0]
                group_end = processed_bounds[group.processed_end - 1][1]
            else:
                # Degenerate group: the original word(s) here produced no
                # spoken output at all (e.g. a stripped emoji). Give it a
                # zero-width slot so the browser still has a timing entry
                # to advance past.
                group_start = last_end
                group_end = last_end

            words = [alignment.original_words[i] for i in group.original_indices]

            # A word in a degenerate group produced NO processed word, so
            # by definition it was never spoken — whether it's an emoji,
            # an invisible character, or a lone punctuation/delimiter
            # token (".", ")", ").") standing between \S+ boundaries.
            # Flag every one of them silent so it becomes a zero-width,
            # non-target marker: without this a trailing "." got a real
            # (if tiny) window right at end-of-audio and the highlight
            # jumped onto it off the final spoken letter. A non-alnum
            # token that DID produce spoken output (e.g. "&" -> "and")
            # lands in a normal group and is correctly left un-silenced.
            #
            # is_non_spoken_word() makes the same call PER WORD for a token
            # that is only dashes, quotes, invisible characters or emoji
            # ("–", '"', NBSP, a zero-width space, "📄"): even if such a
            # token ends up inside a group that does contain spoken words,
            # it stays a zero-width, non-target marker and never takes a
            # share of the spoken word's window.
            silent = [
                degenerate or is_non_spoken_word(w)
                for w in words
            ]
            n_spoken = sum(1 for s in silent if not s)

            # A "silent" original word (emoji/invisible-only — see
            # is_pure_emoji_word) can end up sharing a WordGroup with a
            # real spoken word purely because it produced zero processed
            # output and got folded into whichever group followed it (see
            # alignment.py's _build_groups). The group's `group_start` /
            # `group_end`, however, are the REAL audio bounds of the
            # spoken word(s) alone — dividing that span evenly across
            # every original word in the group (including the silent
            # ones) would eat into the real word's actual spoken window,
            # making the highlight visibly lag behind the audio (it was
            # doing exactly that before this fix). Instead: silent words
            # get a zero-width marker planted at the group's start, and
            # every SPOKEN word in the group shares the group's full,
            # unreduced span — identical to what it would get if no
            # silent word were present at all.
            span = (group_end - group_start) / n_spoken if n_spoken else (
                (group_end - group_start) / len(words)
            )

            cursor = group_start

            for original_index, word, is_silent in zip(group.original_indices, words, silent):

                if is_silent and n_spoken:
                    start = end = group_start
                else:
                    start = cursor
                    end = start + span
                    cursor = end

                timings.append({
                    "word": word,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "emoji": is_silent,
                })

            last_end = group_end

        if duration is not None:
            timings = self.enforce_minimum_duration(timings, duration)

        return timings

    # ------------------------------------------------------------------
    # Minimum-duration enforcement
    #
    # Forced alignment can legitimately give short function words
    # ("to", "is", "at") a very narrow window — sometimes just 1-2
    # acoustic-model frames (~20-40ms). That's *accurate*, but a window
    # that narrow is easy for a polling-based highlighter to miss, and
    # too brief for a reader to register even when it isn't missed. This
    # nudges every word up to a readable floor, without ever pushing the
    # overall timeline past the audio's actual duration.
    #
    # Note: this floor (MIN_WORD_HIGHLIGHT_DURATION, ~80ms) is separate
    # from — and much shorter than — the emoji dwell handled client-side
    # in highlighter.js (~0.5-1s). This one keeps genuinely short SPOKEN
    # words from being missed; the emoji dwell keeps a word that was
    # NEVER spoken at all visible long enough to register.
    # ------------------------------------------------------------------

    def enforce_minimum_duration(self, timings, duration: float, min_duration: float = MIN_WORD_HIGHLIGHT_DURATION):

        if not timings:
            return timings

        adjusted = []
        cursor = 0.0

        for t in timings:

            if t.get("emoji"):
                # Never apply the readable-minimum floor to a silent
                # (emoji/invisible) word, and never let it advance the
                # cursor: doing either would delay the START of the
                # real spoken word that follows it, reintroducing the
                # exact "highlight lags behind the audio" bug this
                # function exists to prevent for everyone else. A
                # zero-width marker at (at least) the cursor position
                # is all it needs — the frontend holds it visible for
                # a fixed audio-clock dwell independently of its
                # recorded duration here (see highlighter.js).
                start = end = max(t["start"], cursor)
                adjusted.append({
                    "word": t["word"],
                    "start": start,
                    "end": end,
                    "emoji": True,
                    "_real_start": t["start"],
                })
                continue

            start = max(t["start"], cursor)
            end = max(t["end"], start + min_duration)
            adjusted.append({
                "word": t["word"],
                "start": start,
                "end": end,
                "emoji": False,
                "_real_start": t["start"],
            })
            cursor = end

        # Enforcing a floor can, in pathological cases (many very short
        # words back to back), push the final word's end past the
        # actual audio duration. This must still be pulled back inside
        # [0, duration] -- but NOT by rescaling every word in the list:
        # that would multiply every timestamp by the same factor,
        # nudging even a word whose forced-alignment timing was already
        # perfectly correct (its start was never touched above) earlier
        # than its real position. The error compounds with elapsed
        # time, so a correction meant for a handful of short words near
        # one spot in the text ends up visibly dragging the highlight
        # ahead of the audio for everything that comes after it.
        #
        # Instead, only compress the TRAILING run of entries whose
        # start actually got pushed later than its real (forced-
        # alignment) value -- see _compress_trailing_debt(). Anything
        # before that run keeps its exact original timing.
        overrun = adjusted[-1]["end"] - duration

        if overrun > 1e-9 and adjusted[-1]["end"] > 0:
            self._compress_trailing_debt(adjusted, overrun, duration)

        for a in adjusted:
            a.pop("_real_start", None)
            a["start"] = round(a["start"], 3)
            a["end"] = round(a["end"], 3)

        return adjusted

    def _compress_trailing_debt(self, adjusted, overrun: float, duration: float):
        """
        Recovers an `overrun`-second shortfall by compressing only the
        trailing run of entries whose start was pushed later than its
        real forced-alignment start (i.e. those that inherited padding
        debt from an earlier too-short word) -- entries before that run
        are left completely untouched, at their exact original timing.

        This keeps a correction triggered by a *local* cluster of very
        short words from bleeding into every other word's timestamp
        (see the comment in enforce_minimum_duration()).
        """

        n = len(adjusted)

        k = n
        while k > 0 and adjusted[k - 1]["start"] - adjusted[k - 1]["_real_start"] > 1e-9:
            k -= 1

        anchor = adjusted[k - 1]["end"] if k > 0 else 0.0
        span_before = adjusted[-1]["end"] - anchor

        if span_before <= 1e-9:
            # The debt run is the entire timeline (or degenerate) --
            # there is no clean prefix to anchor to, so the whole thing
            # has to absorb the correction.
            anchor = 0.0
            span_before = adjusted[-1]["end"]
            k = 0

        span_after = max(0.0, span_before - overrun)
        scale = span_after / span_before if span_before > 1e-9 else 1.0

        for a in adjusted[k:]:
            a["start"] = anchor + (a["start"] - anchor) * scale
            a["end"] = anchor + (a["end"] - anchor) * scale

    # ------------------------------------------------------------------
    # Forced-alignment generation
    #
    # Same idea as generate_from_alignment(), but the per-processed-word
    # (start, end) bounds come from a real acoustic forced-alignment
    # model instead of a naive equal split. Individual words the aligner
    # couldn't place (e.g. a number that failed to spell out) are filled
    # in by interpolating between their nearest aligned neighbors, rather
    # than discarding the whole result.
    # ------------------------------------------------------------------

    def generate_from_forced_alignment(self, alignment: AlignmentResult, word_bounds, duration: float):
        """
        `word_bounds` is a list, one entry per alignment.processed_words,
        of either a WordBound-like object (with .start/.end) or None if
        that word could not be aligned.
        """

        resolved = self._resolve_processed_bounds(word_bounds, duration)

        if resolved is None:
            raise ValueError(
                "Forced alignment produced no usable word bounds "
                "(every word failed to align)."
            )

        return self.generate_from_word_bounds(alignment, resolved, duration)

    def _resolve_processed_bounds(self, word_bounds, duration: float):
        """
        Converts a list of Optional[WordBound] into a fully-populated
        list of (start, end) tuples, interpolating over any None entries
        using their nearest known neighbors on each side.

        Returns None if there isn't a single known bound to anchor to
        (caller should fall back to the naive equal-split entirely).
        """

        n = len(word_bounds)

        resolved = [
            (b.start, b.end) if b is not None else None
            for b in word_bounds
        ]

        if not any(b is not None for b in resolved):
            return None

        i = 0
        while i < n:

            if resolved[i] is not None:
                i += 1
                continue

            gap_start = i
            while i < n and resolved[i] is None:
                i += 1
            gap_end = i  # exclusive

            left_time = resolved[gap_start - 1][1] if gap_start > 0 else 0.0
            right_time = resolved[gap_end][0] if gap_end < n else duration

            gap_size = gap_end - gap_start
            span = (right_time - left_time) / gap_size

            for offset, idx in enumerate(range(gap_start, gap_end)):
                start = left_time + offset * span
                end = start + span
                resolved[idx] = (start, end)

        # Snap to the full audio duration so there's no dead air at the
        # very start/end where nothing would be highlighted.
        resolved[0] = (0.0, resolved[0][1])
        resolved[-1] = (resolved[-1][0], duration)

        return resolved

    def save(self, timings, output_file: Path):

        with open(output_file, "w", encoding="utf-8") as f:

            json.dump(
                timings,
                f,
                indent=4,
                ensure_ascii=False
            )
