import logging
import subprocess
import sys
from pathlib import Path

from cancellation import NULL_TOKEN, GenerationCancelled
from config import (
    PIPER_EXE,
    ESPEAK_DATA,
    MALE_MODEL,
    FEMALE_MODEL,
    OUTPUT_DIR,
    SENTENCE_SILENCE_SECONDS,
)

logger = logging.getLogger(__name__)


class PiperTTS:
    """
    Thin wrapper around the piper.exe CLI.

    IMPORTANT — thread safety: synthesize() is called concurrently from
    multiple threads under the dual-voice generation pipeline (the
    requested voice runs in the foreground while the other voice is
    generated in the background — see speech_pipeline.py). Earlier
    versions of this class resolved the voice model via a mutable
    `self.voice` attribute set by a separate set_voice() call; two
    concurrent synthesize() calls could interleave their set_voice()/
    read-self.voice steps and end up building a Piper command with the
    WRONG model. This version resolves the model path as a local
    variable inside synthesize() instead, so instances of this class
    have no per-call mutable state and are safe to share across
    threads.

    IMPORTANT — encoding: `text` is piped to Piper's stdin via
    subprocess.run(..., text=True). Without an explicit `encoding`,
    Python encodes stdin using the OS's default locale encoding, which
    on Windows is typically a legacy code page (cp1252, surfaced by
    Python as the "charmap" codec) rather than UTF-8. Any character
    outside that code page — an emoji, an accented letter outside
    Western-European coverage, an invisible zero-width character — then
    raises UnicodeEncodeError and crashes the request. `encoding="utf-8"`
    below removes that failure mode entirely, independent of the OS or
    its configured locale.
    """

    def __init__(self):
        pass

    # ----------------------------------------------------

    def _resolve_voice_model(self, voice: str) -> Path:

        if voice.lower() == "female":
            return FEMALE_MODEL

        return MALE_MODEL

    # ----------------------------------------------------

    def synthesize(
        self,
        text: str,
        voice: str = "male",
        speed: float = 1.0,
        output_file: Path = None,
        cancel_token=None,
    ):

        text = text.strip()

        if not text:
            raise ValueError("Text is empty.")

        if output_file is None:
            raise ValueError("output_file is required.")

        output_file = Path(output_file)

        voice_model = self._resolve_voice_model(voice)

        OUTPUT_DIR.mkdir(exist_ok=True)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        if output_file.exists():
            output_file.unlink()

        # --------------------------------------------
        # Piper uses length_scale
        #
        # speed = 1.0  -> length_scale = 1.0
        # speed = 1.5  -> length_scale = 0.666
        # speed = 0.8  -> length_scale = 1.25
        # --------------------------------------------

        if speed <= 0:
            speed = 1.0

        length_scale = 1.0 / speed

        logger.info(
            "Synthesizing speech (voice=%s [%s], speed=%.2fx, "
            "length_scale=%.3f) -> %s",
            voice, voice_model.name, speed, length_scale, output_file
        )

        command = [

            str(PIPER_EXE),

            "--model",
            str(voice_model),

            "--espeak_data",
            str(ESPEAK_DATA),

            "--output_file",
            str(output_file),

            "--length_scale",
            str(length_scale),

            # Without this, Piper falls back to its own (short) default
            # sentence-end pause — see config.py's SENTENCE_SILENCE_SECONDS
            # comment for why that's the actual cause of a heading's
            # inserted period producing no audible gap in the audio.
            "--sentence_silence",
            str(SENTENCE_SILENCE_SECONDS)

        ]

        token = cancel_token if cancel_token is not None else NULL_TOKEN

        # Don't even launch Piper if this generation was already
        # abandoned while we were resolving paths above.
        token.raise_if_cancelled()

        # host.py runs under pythonw.exe (no console of its own), so
        # without this flag every synthesis call still flashes up a
        # brand-new console window for piper.exe — same CREATE_NO_WINDOW
        # pattern used for child processes in launcher/environment.py and
        # launcher/server.py.
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        # Popen + communicate(), NOT subprocess.run(): run() blocks with
        # no handle to the child, so an abandoned generation would keep
        # a full synthesis running to completion with no way to stop it.
        # Registering the process on the token is what lets a NEWER
        # navigation actually kill this one and get the CPU back
        # immediately (see cancellation.py).
        try:

            process = subprocess.Popen(

                command,

                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,

                text=True,

                # Explicit UTF-8 for stdin/stdout/stderr — see the class
                # docstring. This is what actually prevents the
                # 'charmap' codec crashes on Windows.
                encoding="utf-8",

                # Belt-and-braces: UTF-8 can represent every valid
                # Unicode code point, so this should never trigger in
                # practice, but a corrupt/malformed input character
                # degrades to a replacement glyph instead of crashing
                # the request.
                errors="replace",

                creationflags=creationflags,

            )

        except FileNotFoundError as exc:

            logger.exception("Piper executable not found at %s.", PIPER_EXE)

            raise RuntimeError(
                f"Piper executable not found: {PIPER_EXE}"
            ) from exc

        except OSError as exc:

            logger.exception("Failed to launch the Piper subprocess.")

            raise RuntimeError(f"Failed to launch Piper: {exc}") from exc

        token.register_process(process)

        try:
            _stdout, stderr = process.communicate(input=text)

        except OSError as exc:
            # Killing the child mid-write can break the stdin pipe
            # (BrokenPipeError / EINVAL on Windows) instead of returning
            # cleanly. When that was OUR kill, it's a cancellation, not
            # an I/O fault — check the token before blaming Piper.
            token.raise_if_cancelled()

            logger.exception("Piper subprocess I/O failed.")

            raise RuntimeError(f"Piper subprocess I/O failed: {exc}") from exc

        finally:
            token.clear_process()

        # Checked BEFORE the returncode test below: a killed process
        # exits non-zero, and reporting that as "Piper failed" would turn
        # a deliberate cancellation into a spurious error in the logs and
        # a 500 to a client that has already moved on.
        token.raise_if_cancelled()

        if process.returncode != 0:

            logger.error(
                "Piper exited with code %s: %s",
                process.returncode, stderr
            )

            raise RuntimeError(
                stderr or f"Piper exited with code {process.returncode}."
            )

        if not output_file.exists():

            logger.error(
                "Piper reported success but did not produce %s.",
                output_file.name
            )

            raise RuntimeError(f"{output_file.name} was not generated.")

        logger.info("Synthesis complete: %s", output_file.name)

        return output_file
