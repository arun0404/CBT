"""
Orchestrates the full text -> speech -> timings pipeline, including
DUAL-VOICE generation: whichever voice was requested is generated (or
served from cache) in the foreground with no added delay, and the
OTHER voice is generated in the background immediately afterward so
that switching voices later is an instant cache hit.

    male requested  -> serve male now, prefetch female in background
    female requested -> serve female now, prefetch male in background

Responsibilities split from host.py so the Flask route stays a thin
HTTP adapter:
  - cache lookups (cache.py)
  - Piper synthesis + forced alignment for cache misses
  - de-duplication of concurrent identical generation requests (two
    threads should never redundantly synthesize/align the exact same
    text/voice/speed at the same time)
  - the "never cache fallback" rule: if forced alignment fails and we
    fall back to naive equal-duration timing, that result is served
    from a small ephemeral (non-reusable) location instead of being
    written into the real cache — see cache.py's docstring.
"""

import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import soundfile as sf

from cache import AlignmentCache
from cancellation import NULL_TOKEN, GenerationCancelled
from config import (
    ALIGNMENT_CACHE_URL_PREFIX,
    ALIGNMENT_CACHE_VERSION,
    EPHEMERAL_DIR,
    EPHEMERAL_MAX_ENTRIES,
    EPHEMERAL_URL_PREFIX,
    GENERATION_WAIT_TIMEOUT_SECONDS,
    SCRATCH_DIR,
    VOICE_FINGERPRINT,
)
from piper import PiperTTS
from text_processing.preprocess import preprocessor
from timing import TimingGenerator

logger = logging.getLogger(__name__)

VOICES = ("male", "female")


def other_voice(voice: str) -> str:
    """The voice to proactively prefetch given the one that was requested."""
    return "female" if voice.lower() == "male" else "male"


class SpeechPipeline:

    def __init__(
        self,
        cache: AlignmentCache,
        tts: PiperTTS,
        forced_aligner,
        forced_alignment_available: bool,
    ):

        self._cache = cache
        self._tts = tts
        self._forced_aligner = forced_aligner
        self._forced_alignment_available = forced_alignment_available

        self._scratch_dir = SCRATCH_DIR
        self._scratch_dir.mkdir(parents=True, exist_ok=True)
        self._clear_dir(self._scratch_dir)

        self._ephemeral_dir = EPHEMERAL_DIR
        self._ephemeral_dir.mkdir(parents=True, exist_ok=True)
        self._clear_dir(self._ephemeral_dir)
        self._ephemeral_lock = threading.Lock()
        self._ephemeral_entries: "OrderedDict[str, None]" = OrderedDict()
        self._ephemeral_max_entries = EPHEMERAL_MAX_ENTRIES

        # Coordinates concurrent generation attempts for the same cache
        # key (e.g. a foreground request for "female" arriving while a
        # background prefetch for "female" is already in flight).
        self._inflight_lock = threading.Lock()
        self._inflight_events: "dict[str, threading.Event]" = {}

        logger.info(
            "SpeechPipeline initialized (forced_alignment_available=%s).",
            forced_alignment_available,
        )

    def set_forced_aligner(self, forced_aligner, available: bool) -> None:
        """
        Swap in the forced aligner once its background load completes.

        host.py constructs this pipeline with forced_aligner=None /
        available=False so that Flask can bind its socket immediately
        on startup, then loads the real MMS aligner on a daemon thread
        and calls this method once that finishes (whether it succeeded
        or fell back) — see host.py's _load_forced_aligner().

        Reassigning these two attributes needs no lock: under the GIL
        each assignment is atomic, and every request thread only ever
        *reads* self._forced_aligner / self._forced_alignment_available
        (see _generate() below) — never mutates them. Worst case, a
        request that started microseconds before this call still sees
        the old (fallback) values for that one request, which is
        correct behaviour, just lower-fidelity timing until the next
        request picks up the update.
        """

        self._forced_aligner = forced_aligner
        self._forced_alignment_available = available

        logger.info(
            "SpeechPipeline forced_alignment_available updated to %s.",
            available,
        )

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def handle_request(self, original_text: str, voice: str, speed: float, cancel_token=None) -> dict:
        """
        Produces the response payload for `voice` — generating it if
        necessary, otherwise serving straight from cache — and fires
        off background generation of the OTHER voice so a later voice
        switch for the same text/speed is instant. Never blocks on the
        background job.

        `cancel_token` (see cancellation.py) is shared by BOTH the
        foreground generation and the background secondary prefetch, so
        a newer navigation abandons all of this page's work together
        rather than leaving the background half running.

        Returns {"audio": url, "timing": url, "timings": list, "cached": bool}.

        Raises GenerationCancelled if the token is cancelled mid-flight.
        """

        token = cancel_token if cancel_token is not None else NULL_TOKEN

        token.raise_if_cancelled()

        # Preprocessing (abbreviation/unit/symbol expansion) doesn't
        # depend on voice, so it's computed once and reused for both
        # the primary and the background secondary generation.
        alignment = preprocessor.process_with_alignment(original_text)

        primary = self._get_or_generate(
            original_text, alignment, voice, speed, blocking=True, cancel_token=token
        )

        secondary_voice = other_voice(voice)

        threading.Thread(
            target=self._prefetch_secondary,
            args=(original_text, alignment, secondary_voice, speed, token),
            name=f"prefetch-{secondary_voice}",
            daemon=True,
        ).start()

        return primary

    # ------------------------------------------------------------
    # Background prefetch of the other voice
    # ------------------------------------------------------------

    def _prefetch_secondary(self, original_text, alignment, voice: str, speed: float, cancel_token=None) -> None:

        token = cancel_token if cancel_token is not None else NULL_TOKEN

        try:
            token.raise_if_cancelled()

            key = self._make_key(original_text, voice, speed)

            if self._cache.get(key) is not None:
                logger.info(
                    "Secondary voice '%s' already cached (%s); nothing to prefetch.",
                    voice, key[:12]
                )
                return

            logger.info(
                "Prefetching secondary voice '%s' in the background (%s).",
                voice, key[:12]
            )

            result = self._get_or_generate(
                original_text, alignment, voice, speed,
                blocking=False, precomputed_key=key, cancel_token=token,
            )

            if result is None:
                logger.info(
                    "Secondary voice '%s' prefetch skipped — already being "
                    "generated by another request.", voice
                )
            elif result["cached"]:
                logger.info(
                    "Secondary voice '%s' prefetch complete and cached (%s). "
                    "Switching to this voice will now be instant.",
                    voice, key[:12]
                )
            else:
                logger.info(
                    "Secondary voice '%s' prefetch fell back to uncached "
                    "timing; a future request will retry real alignment.",
                    voice
                )

        except GenerationCancelled:
            # Expected whenever the user navigates away — this thread's
            # whole reason to exist went with the page. Not a failure,
            # so deliberately not logged at exception level.
            logger.info(
                "Background prefetch of '%s' cancelled — navigated away.", voice
            )

        except Exception:
            logger.exception("Background prefetch failed for voice '%s'.", voice)

    # ------------------------------------------------------------
    # Cache lookup + generation, with in-flight de-duplication
    # ------------------------------------------------------------

    def _get_or_generate(
        self,
        original_text: str,
        alignment,
        voice: str,
        speed: float,
        blocking: bool,
        precomputed_key: Optional[str] = None,
        cancel_token=None,
    ) -> Optional[dict]:
        """
        If `blocking` is True (the requested/foreground voice), this
        always returns a servable result — waiting for and reusing any
        identical in-flight generation rather than duplicating work,
        and generating independently as a last resort.

        If `blocking` is False (a background prefetch), this returns
        None immediately when an identical generation is already in
        flight, rather than waiting — the other caller will populate
        the cache and this prefetch has nothing useful left to do.
        """

        token = cancel_token if cancel_token is not None else NULL_TOKEN

        key = precomputed_key or self._make_key(original_text, voice, speed)

        # One claim attempt, then (for blocking callers) one retry after
        # waiting for whoever holds the claim to finish.
        for _ in range(2):

            # A cache hit is still served even if cancelled — it costs
            # nothing and the caller may legitimately still want it. Real
            # WORK, below, is what checkpoints guard.
            cached = self._cache.get(key)
            if cached is not None:
                return self._response_for(
                    voice, cached=True,
                    audio_url=self._cache_url(key, ".wav"),
                    timing_url=self._cache_url(key, ".json"),
                    timings=cached.timings,
                )

            token.raise_if_cancelled()

            if self._claim(key):
                try:
                    return self._generate(
                        original_text, alignment, voice, speed, key, token
                    )
                finally:
                    self._release(key)

            if not blocking:
                return None

            self._wait(key, timeout=GENERATION_WAIT_TIMEOUT_SECONDS)

        # Still contended after a retry (very rare — e.g. rapid
        # back-to-back identical requests). Generate without an
        # exclusive claim rather than block the user's request
        # indefinitely; worst case this duplicates a small amount of
        # work with whichever other thread is still holding the claim.
        token.raise_if_cancelled()

        return self._generate(original_text, alignment, voice, speed, key, token)

    def _make_key(self, original_text: str, voice: str, speed: float) -> str:
        return self._cache.make_key(
            text=original_text,
            voice=voice,
            speed=speed,
            dictionary_fingerprint=preprocessor.dictionary_fingerprint,
            voice_fingerprint=VOICE_FINGERPRINT,
            algorithm_version=ALIGNMENT_CACHE_VERSION,
        )

    # ------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------

    def _generate(self, original_text, alignment, voice: str, speed: float, key: str, cancel_token=None) -> dict:

        token = cancel_token if cancel_token is not None else NULL_TOKEN

        scratch_wav = self._scratch_dir / f"{uuid.uuid4().hex}.wav"

        try:
            # piper.py checks the token itself and registers its
            # subprocess on it, so an abandoned synthesis is KILLED
            # rather than merely stopped at the next checkpoint.
            self._tts.synthesize(
                text=alignment.processed_text,
                voice=voice,
                speed=speed,
                output_file=scratch_wav,
                cancel_token=token,
            )

            # Checkpoint between the two expensive stages: forced
            # alignment is a full acoustic-model forward pass, so a
            # generation abandoned during synthesis must not go on to
            # spend that too.
            token.raise_if_cancelled()

            audio, sample_rate = sf.read(scratch_wav)
            duration = len(audio) / sample_rate

            generator = TimingGenerator()
            timings = None

            if self._forced_alignment_available:
                try:
                    word_bounds = self._forced_aligner.align(
                        scratch_wav, alignment.processed_words
                    )
                    timings = generator.generate_from_forced_alignment(
                        alignment, word_bounds, duration
                    )
                    logger.info(
                        "Forced-alignment succeeded for voice '%s' (%s).",
                        voice, key[:12]
                    )
                except GenerationCancelled:
                    # MUST be re-raised ahead of the generic handler
                    # below: without this, a cancellation raised inside
                    # alignment would be swallowed as an "alignment
                    # failed" and the abandoned generation would carry
                    # on to write an ephemeral fallback entry to disk —
                    # precisely the partial/unwanted artifact
                    # cancellation exists to prevent.
                    raise
                except Exception as exc:
                    logger.warning(
                        "Forced alignment failed for voice '%s' (%s); "
                        "using equal-duration fallback.",
                        voice, exc
                    )

            # Last checkpoint before anything is written to disk, so an
            # abandoned generation never lands in the cache or the
            # ephemeral slot at all.
            token.raise_if_cancelled()

            if timings is not None:
                self._cache.set(key, timings, scratch_wav)
                return self._response_for(
                    voice, cached=True,
                    audio_url=self._cache_url(key, ".wav"),
                    timing_url=self._cache_url(key, ".json"),
                    timings=timings,
                )

            # Fallback: deliberately NOT written to the reusable cache
            # (see cache.py) — served from an ephemeral slot instead so
            # a future identical request still gets a fresh shot at
            # real forced alignment.
            timings = generator.generate_from_alignment(alignment, duration)
            return self._serve_ephemeral(scratch_wav, timings, voice)

        finally:
            # In the cached path, cache.set() has already COPIED the
            # scratch file, so this cleans up the original. In the
            # ephemeral path, _serve_ephemeral() already MOVED the
            # scratch file, so this is a safe no-op.
            scratch_wav.unlink(missing_ok=True)

    # ------------------------------------------------------------
    # Ephemeral (uncached-fallback) serving
    # ------------------------------------------------------------

    def _serve_ephemeral(self, scratch_wav: Path, timings: list, voice: str) -> dict:

        entry_id = uuid.uuid4().hex
        audio_dest = self._ephemeral_dir / f"{entry_id}.wav"
        json_dest = self._ephemeral_dir / f"{entry_id}.json"

        os.replace(scratch_wav, audio_dest)  # move — scratch is single-use

        with open(json_dest, "w", encoding="utf-8") as f:
            json.dump(timings, f, ensure_ascii=False)

        self._track_ephemeral(entry_id)

        logger.info(
            "Serving uncached (fallback) timing for voice '%s' from "
            "ephemeral entry %s.", voice, entry_id
        )

        return self._response_for(
            voice, cached=False,
            audio_url=f"{EPHEMERAL_URL_PREFIX}/{entry_id}.wav",
            timing_url=f"{EPHEMERAL_URL_PREFIX}/{entry_id}.json",
            timings=timings,
        )

    def _track_ephemeral(self, entry_id: str) -> None:

        with self._ephemeral_lock:
            self._ephemeral_entries[entry_id] = None
            self._ephemeral_entries.move_to_end(entry_id)

            while len(self._ephemeral_entries) > self._ephemeral_max_entries:
                oldest_id, _ = self._ephemeral_entries.popitem(last=False)
                for suffix in (".wav", ".json"):
                    (self._ephemeral_dir / f"{oldest_id}{suffix}").unlink(missing_ok=True)

    # ------------------------------------------------------------
    # In-flight de-duplication
    # ------------------------------------------------------------

    def _claim(self, key: str) -> bool:
        with self._inflight_lock:
            if key in self._inflight_events:
                return False
            self._inflight_events[key] = threading.Event()
            return True

    def _release(self, key: str) -> None:
        with self._inflight_lock:
            event = self._inflight_events.pop(key, None)
        if event is not None:
            event.set()

    def _wait(self, key: str, timeout: float) -> None:
        with self._inflight_lock:
            event = self._inflight_events.get(key)
        if event is not None:
            event.wait(timeout)

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    @staticmethod
    def _cache_url(key: str, suffix: str) -> str:
        return f"{ALIGNMENT_CACHE_URL_PREFIX}/{key}{suffix}"

    @staticmethod
    def _response_for(voice: str, cached: bool, audio_url: str, timing_url: str, timings: list) -> dict:
        # `timings` is embedded directly (not just `timing_url`) so the
        # client can skip the follow-up GET entirely — every caller of
        # this method already has the list sitting in memory (read from
        # cache.py's CachedSpeech, or just-generated locally), so this
        # costs nothing extra to include. `timing_url` is kept too: it's
        # still the correct, content-addressed identity for the browser's
        # own HTTP cache (see api.js's getTimings, kept as a fallback
        # path) and for anything that wants to re-fetch it directly.
        return {
            "audio": audio_url,
            "timing": timing_url,
            "timings": timings,
            "voice": voice,
            "cached": cached,
        }

    @staticmethod
    def _clear_dir(path: Path) -> None:
        # Scratch/ephemeral files are request-scoped and never meant to
        # persist across restarts — clear out anything left behind by
        # a prior process (e.g. after a crash).
        for entry in path.iterdir():
            try:
                if entry.is_file():
                    entry.unlink()
            except OSError:
                logger.warning("Could not remove stale file %s during startup cleanup.", entry)
