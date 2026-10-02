"""
Real per-word timing via torchaudio's MMS_FA forced-alignment model.

Replaces the "assume every word takes the same amount of time to speak"
approximation (timing.TimingGenerator.generate_from_alignment) with
actual acoustic alignment: the synthesized audio is run back through a
forced-alignment model together with the text that was spoken, and the
model tells us exactly which time span each word occupies.

--------------------------------------------------------------------
IMPORTANT — torchaudio API stability
--------------------------------------------------------------------
This module uses torchaudio.pipelines.MMS_FA and the forced-alignment
functions it wraps (torchaudio.functional.forced_align / merge_tokens).
As of torchaudio 2.8, PyTorch announced these are deprecated and will
be removed once decoding/encoding moves fully to TorchCodec:
https://github.com/pytorch/audio/issues/3902

They are still present and functional as of torchaudio 2.11 (verified
directly against the installed package while building this module).
Pin your torchaudio version in requirements.txt and re-verify this
module (see test_forced_aligner.py) before upgrading torchaudio.
--------------------------------------------------------------------

IMPORTANT — vocabulary constraint
--------------------------------------------------------------------
MMS_FA's tokenizer only knows lowercase a-z, apostrophe, and a couple
of special tokens (confirmed via bundle.get_dict()). It has NO digit
tokens, so "25", "220", "4.2" etc. must be spelled out before being
handed to the tokenizer, or tokenization raises KeyError. This module
does that automatically via num2words; see _normalize_word().
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import torch
from num2words import num2words

from aligner.model import alignment_model
from aligner.audio import audio_processor

logger = logging.getLogger(__name__)

# MMS_FA's vocabulary is lowercase a-z + apostrophe + space (space is
# just our own word separator, not a model token). Anything else must
# be stripped or spelled out before tokenization.
_ALIGN_CHAR_RE = re.compile(r"[^a-z' ]")
_NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")

# A digit run EMBEDDED in a token that isn't itself a pure number — e.g.
# the "79", "24", "00", "870", "7" inside a hyphenated reference/task
# code like "79-24-00-870-7A" (protected verbatim by preprocess.py's
# protect_reference_codes, so it reaches here as ONE processed word).
# _NUMBER_RE only spells out a word that is NOTHING BUT digits; without
# this second pattern, every digit in a mixed token like that one gets
# silently deleted by _ALIGN_CHAR_RE below, leaving the aligner almost
# nothing to anchor to (here, just the trailing "a") -- see
# _normalize_word.
_EMBEDDED_DIGIT_RUN_RE = re.compile(r"\d+")


class AlignmentError(Exception):
    """
    Raised when forced alignment cannot be computed at all for a given
    utterance. Callers should catch this and fall back to
    TimingGenerator.generate_from_alignment() (the naive equal split).
    """


@dataclass
class WordBound:
    start: float
    end: float
    score: float


class ForcedAligner:
    """
    Thin orchestration layer around torchaudio's MMS_FA bundle.

    Reuses the model/bundle already loaded by aligner.model.alignment_model
    (a singleton, loaded once at process startup) rather than loading a
    second copy of the weights.

    Usage:
        bounds = forced_aligner.align(wav_path, processed_words)
        # bounds[i] is a WordBound for processed_words[i], or None if
        # that particular word could not be aligned.
    """

    def __init__(self):

        self._model = alignment_model.get_model()
        self._bundle = alignment_model.get_bundle()
        self._device = alignment_model.get_device()
        self._sample_rate = alignment_model.get_sample_rate()

        self._tokenizer = self._bundle.get_tokenizer()
        self._aligner = self._bundle.get_aligner()

    # ------------------------------------------------------------
    # Text normalization
    # ------------------------------------------------------------

    def _normalize_word(self, word: str) -> List[str]:
        """
        Converts one PROCESSED word into 0+ alignment-vocabulary
        sub-tokens. Numbers are spelled out (the model has no digit
        tokens); everything else is lowercased with punctuation the
        model doesn't know stripped out.

        Returns an empty list if nothing alignable remains (the caller
        treats that word as "could not be aligned" and interpolates
        its timing from neighboring words instead).
        """

        if _NUMBER_RE.match(word):
            try:
                value = float(word) if "." in word else int(word)
                word_for_alignment = num2words(value)
            except (ValueError, OverflowError) as exc:
                logger.warning("Could not spell out %r for alignment: %s", word, exc)
                return []
        elif _EMBEDDED_DIGIT_RUN_RE.search(word):
            # A digit run embedded in a token that ISN'T itself a pure
            # number (e.g. "79-24-00-870-7A") -- spell out each run
            # individually rather than falling through to the plain
            # else-branch below, which would leave _ALIGN_CHAR_RE to
            # silently delete every digit. That used to reduce this
            # whole reference code to a single alignable letter ("a"),
            # so the aligner had nothing to anchor the word's real
            # START to and placed its whole timing window right where
            # that lone letter is actually spoken -- near the very end
            # of the code -- instead of spanning its true start-to-end.
            # See ALIGNMENT_CACHE_VERSION history in config.py.
            def _spell(match: "re.Match[str]") -> str:
                try:
                    return num2words(int(match.group(0)))
                except (ValueError, OverflowError) as exc:
                    logger.warning(
                        "Could not spell out embedded digits %r in %r for "
                        "alignment: %s", match.group(0), word, exc
                    )
                    return ""

            word_for_alignment = _EMBEDDED_DIGIT_RUN_RE.sub(
                lambda m: f" {_spell(m)} ", word
            )
        else:
            word_for_alignment = word

        word_for_alignment = word_for_alignment.lower().replace("\u2019", "'")
        word_for_alignment = _ALIGN_CHAR_RE.sub(" ", word_for_alignment)

        return word_for_alignment.split()

    def _build_transcript(
        self, processed_words: List[str]
    ) -> Tuple[List[str], List[Tuple[int, int]]]:
        """
        Flattens processed_words into the list of sub-tokens the aligner
        actually sees, plus the [start, end) slice of that flat list each
        processed word maps to (end == start means "not alignable").
        """

        flat_subtokens: List[str] = []
        ranges: List[Tuple[int, int]] = []

        for word in processed_words:
            subtokens = self._normalize_word(word)
            start = len(flat_subtokens)
            flat_subtokens.extend(subtokens)
            ranges.append((start, len(flat_subtokens)))

        return flat_subtokens, ranges

    # ------------------------------------------------------------
    # Alignment
    # ------------------------------------------------------------

    def align(self, wav_path, processed_words: List[str]) -> List[Optional[WordBound]]:
        """
        Returns one entry per item in processed_words: a WordBound, or
        None if that specific word couldn't be placed (e.g. it produced
        no alignable characters).

        Raises AlignmentError if the model fails on the whole utterance
        (bad audio, empty transcript, unexpected torchaudio error) —
        callers should catch this and fall back to the naive timing.
        """

        if not processed_words:
            return []

        flat_subtokens, ranges = self._build_transcript(processed_words)

        if not flat_subtokens:
            raise AlignmentError(
                "None of the processed words produced alignable text "
                "(check for unusual characters or a normalization bug)."
            )

        try:
            waveform, sample_rate = audio_processor.load(wav_path)
            waveform = waveform.to(self._device)

            with torch.inference_mode():
                emission, _ = self._model(waveform)
                token_spans = self._aligner(
                    emission[0], self._tokenizer(flat_subtokens)
                )

        except AlignmentError:
            raise
        except Exception as exc:
            raise AlignmentError(f"MMS forced alignment failed: {exc}") from exc

        num_frames = emission.size(1)
        ratio = waveform.size(1) / num_frames

        bounds: List[Optional[WordBound]] = []

        for start_idx, end_idx in ranges:

            if start_idx == end_idx:
                bounds.append(None)
                continue

            char_spans = [
                span
                for sub_word_spans in token_spans[start_idx:end_idx]
                for span in sub_word_spans
            ]

            if not char_spans:
                bounds.append(None)
                continue

            start_frame = char_spans[0].start
            end_frame = char_spans[-1].end

            start_time = (ratio * start_frame) / sample_rate
            end_time = (ratio * end_frame) / sample_rate

            total_len = sum(len(s) for s in char_spans)
            score = (
                sum(s.score * len(s) for s in char_spans) / total_len
                if total_len > 0 else 0.0
            )

            bounds.append(WordBound(start=start_time, end=end_time, score=score))

        return bounds


# Loaded once per process, at import time — mirrors the pattern already
# used by aligner.model.alignment_model.
forced_aligner = ForcedAligner()