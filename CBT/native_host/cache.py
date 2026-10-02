"""
Cache for complete, ready-to-serve speech results.

Forced alignment (aligner/forced_aligner.py) and Piper synthesis are
both real work — alignment is a full acoustic-model forward pass on
CPU, synthesis shells out to piper.exe. Piper is deterministic, so the
same (text, voice, speed, dictionaries, voice models, algorithm) always
produces the same audio and therefore the same correct alignment: it's
always safe to reuse a cached result for an identical request.

Unlike the original version of this module, a cache entry now stores
BOTH the timings *and* the synthesized audio (see CachedSpeech). This
is what makes instant voice-switching possible: a cache hit means
nothing needs to be regenerated at all — not even a Piper resynthesis
— the browser is simply pointed at the already-encoded WAV file and
its previously-computed timings.

Two tiers:
  - In-memory (OrderedDict, LRU-evicted): near-instant repeat hits
    within the same process. Only lightweight bundle objects (a list
    of timing dicts + a Path) are held in memory — the audio bytes
    themselves always live on disk and are streamed by Flask.
  - On-disk (one WAV + one JSON file per entry): survives server
    restarts, so a chapter aligned yesterday doesn't need to be
    re-aligned or re-synthesized today.

This only caches FINAL, trustworthy per-original-word timings — not
partial/failed alignments. If a request fell back to the naive
equal-split timing, it is deliberately NOT cached here, so a later
identical request gets a fresh chance at real forced alignment rather
than being stuck with the fallback. (Callers are expected to serve
fallback results from an ephemeral, non-reusable location instead —
see speech_pipeline.py.)

Thread-safety: this class is accessed concurrently by the primary
(foreground) generation for one voice and the background prefetch of
the other voice, so all shared state is guarded by a lock and all disk
writes are atomic (write-to-temp + rename) so a concurrent reader can
never observe a partially-written file.
"""

import hashlib
import json
import logging
import os
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedSpeech:
    """
    A complete, ready-to-serve speech result.

    `audio_path` points at the immutable, content-addressed WAV file on
    disk (never mutated after creation — a new request that would
    produce different audio always gets a different cache key).
    """
    timings: List[dict]
    audio_path: Path


class AlignmentCache:

    def __init__(self, cache_dir: Path, max_memory_entries: int = 256):

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._max_memory_entries = max_memory_entries
        self._memory: "OrderedDict[str, CachedSpeech]" = OrderedDict()

        # Guards both the in-memory dict and the read-modify-write
        # sequences used when writing to disk. Disk reads (get) don't
        # need it — files are only ever replaced atomically, never
        # edited in place, so a concurrent read always sees either the
        # old complete file or the new complete file, never a partial
        # one.
        self._lock = threading.RLock()

        self._cleanup_stale_tmp_files()

    # ------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------

    def make_key(
        self,
        text: str,
        voice: str,
        speed: float,
        dictionary_fingerprint: str,
        voice_fingerprint: str,
        algorithm_version: str,
    ) -> str:
        """
        Every input that could change the CORRECT audio+timing output
        for a given piece of text is folded into the key:
          - text/voice/speed: the actual TTS request
          - dictionary_fingerprint: busts the cache if any abbreviation
            /unit/symbol dictionary is edited (preprocess.py)
          - voice_fingerprint: busts the cache if the Piper executable
            or either voice model file is swapped out, even though the
            "voice" string (male/female) didn't change
          - algorithm_version: busts the cache if the alignment/timing
            algorithm itself changes (bump config.ALIGNMENT_CACHE_VERSION)
        """

        payload = json.dumps(
            {
                "text": text,
                "voice": voice,
                "speed": round(float(speed), 3),
                "dictionary_fingerprint": dictionary_fingerprint,
                "voice_fingerprint": voice_fingerprint,
                "algorithm_version": algorithm_version,
            },
            sort_keys=True
        )

        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------

    def get(self, key: str) -> Optional[CachedSpeech]:

        with self._lock:
            cached = self._memory.get(key)
            if cached is not None:
                self._memory.move_to_end(key)
                logger.info("Alignment cache hit (memory): %s", key[:12])
                return cached

        audio_path = self._audio_path(key)
        json_path = self._json_path(key)

        if not (audio_path.exists() and json_path.exists()):
            return None

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                timings = json.load(f)

        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Corrupt alignment cache entry %s (%s); ignoring.",
                key[:12], exc
            )
            return None

        bundle = CachedSpeech(timings=timings, audio_path=audio_path)

        with self._lock:
            self._store_in_memory(key, bundle)

        logger.info("Alignment cache hit (disk): %s", key[:12])

        return bundle

    def set(self, key: str, timings: List[dict], audio_source_path: Path) -> CachedSpeech:
        """
        Persists a trustworthy (non-fallback) result: copies the audio
        from `audio_source_path` into the cache directory and writes
        the timings alongside it. Both writes are atomic (temp file +
        rename) so a concurrent GET for the same key can never observe
        a half-written file.
        """

        audio_dest = self._audio_path(key)
        json_dest = self._json_path(key)

        tmp_audio = audio_dest.with_suffix(audio_dest.suffix + ".tmp")
        tmp_json = json_dest.with_suffix(json_dest.suffix + ".tmp")

        try:
            shutil.copyfile(audio_source_path, tmp_audio)
            os.replace(tmp_audio, audio_dest)

            with open(tmp_json, "w", encoding="utf-8") as f:
                json.dump(timings, f, ensure_ascii=False)
            os.replace(tmp_json, json_dest)

        finally:
            # Best-effort cleanup if something failed mid-write.
            tmp_audio.unlink(missing_ok=True)
            tmp_json.unlink(missing_ok=True)

        bundle = CachedSpeech(timings=timings, audio_path=audio_dest)

        with self._lock:
            self._store_in_memory(key, bundle)

        logger.info("Alignment cache SET (disk+memory): %s", key[:12])

        return bundle

    def invalidate(self, key: str) -> None:

        with self._lock:
            self._memory.pop(key, None)

        self._audio_path(key).unlink(missing_ok=True)
        self._json_path(key).unlink(missing_ok=True)

    def clear(self) -> None:

        with self._lock:
            self._memory.clear()

        for pattern in ("*.wav", "*.json"):
            for entry in self.cache_dir.glob(pattern):
                entry.unlink(missing_ok=True)

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _store_in_memory(self, key: str, bundle: CachedSpeech) -> None:
        # Caller must hold self._lock.

        self._memory[key] = bundle
        self._memory.move_to_end(key)

        while len(self._memory) > self._max_memory_entries:
            self._memory.popitem(last=False)

    def _audio_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.wav"

    def _json_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _cleanup_stale_tmp_files(self) -> None:
        # A process crash mid-write could leave a stray *.tmp file
        # behind. It was never renamed into place, so it was never a
        # valid cache entry — safe to remove at startup.
        for entry in self.cache_dir.glob("*.tmp"):
            try:
                entry.unlink()
            except OSError:
                logger.warning("Could not remove stale temp file %s.", entry)
