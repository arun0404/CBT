import os
import sys
import threading

# ----------------------------------------------------------------------
# Force UTF-8 on stdout/stderr at process startup.
#
# On Windows, the default encoding for a Python process's text streams
# (console output AND anything piped through them, e.g. the logging
# module's default StreamHandler) is the system's legacy code page —
# reported by Python as the "charmap" codec — not UTF-8. Any print() or
# log call anywhere in the process, including third-party libraries,
# that emits a character outside that code page (an emoji such as
# U+1F4C4 "📄", an invisible formatting character such as U+200B, or
# simply a non-Western-European accented letter) raises:
#
#     UnicodeEncodeError: 'charmap' codec can't encode character ...
#
# and crashes whatever request thread triggered it. Reconfiguring both
# streams to UTF-8 here — before any other module has a chance to print
# anything — removes that entire class of crash.
#
# `errors="replace"` is a last-resort safety net: UTF-8 can represent
# every valid Unicode code point, so this should never actually trigger,
# but if it ever does (e.g. a lone surrogate slipping through from some
# upstream source), it degrades to a visible replacement character
# instead of raising.
#
# `reconfigure()` was added in Python 3.7 and only exists on real text
# I/O streams; the hasattr guard keeps this safe even if sys.stdout/
# stderr is ever swapped out for something that doesn't support it
# (e.g. certain WSGI/embedding setups).
# ----------------------------------------------------------------------

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import logging

from flask import Flask, send_from_directory, request, jsonify
from flask_cors import CORS

from cache import AlignmentCache
from cancellation import CancellationRegistry, GenerationCancelled
from speech_pipeline import SpeechPipeline, VOICES

from config import (
    HTML_DIR,
    OUTPUT_DIR,
    ALIGNMENT_CACHE_DIR,
    ALIGNMENT_CACHE_MAX_MEMORY_ENTRIES,
)

from piper import PiperTTS


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


alignment_cache = AlignmentCache(
    cache_dir=ALIGNMENT_CACHE_DIR,
    max_memory_entries=ALIGNMENT_CACHE_MAX_MEMORY_ENTRIES,
)


app = Flask(
    __name__,
    static_folder=str(HTML_DIR),
    static_url_path=""
)

CORS(app)

tts = PiperTTS()

# Orchestrates cache lookups, generation, and the dual-voice background
# prefetch described in speech_pipeline.py. Constructed with the
# aligner unavailable; the background loader below swaps in the real
# aligner (if any) once it finishes loading, via pipeline.set_forced_aligner().
pipeline = SpeechPipeline(
    cache=alignment_cache,
    tts=tts,
    forced_aligner=None,
    forced_alignment_available=False,
)

# Tracks in-flight generations so a newer navigation can cancel older
# ones — see cancellation.py for why this is driven by an explicit
# client-supplied epoch rather than by trying to detect the client's
# disconnect (which WSGI does not reliably surface until the handler
# writes its response, long after the expensive work is already done).
cancellation_registry = CancellationRegistry()


# ----------------------------------------------------
# Forced alignment is an optional enhancement over the naive
# equal-duration timing. Its model load is expensive (and, on first
# run, requires downloading MMS_FA weights).
#
# Loading it at import time — as the previous version of this file
# did — blocks the Flask socket bind until the model finishes loading,
# which delays the whole UI on every single start (this is also what
# makes a fixed-delay launcher script necessary in the first place).
#
# Instead it is loaded on a background thread: Flask binds immediately,
# the app is usable in ~1-2s, and alignment quality upgrades itself a
# few seconds later once the thread finishes. Requests that arrive
# before the model is ready take the same equal-duration fallback path
# that already exists for machines where the model is unavailable
# entirely (see cache.py's "never cache fallback" rule and
# speech_pipeline.py), so this introduces no new failure mode — only a
# short window where results use the lower-fidelity timing.
# ----------------------------------------------------

FORCED_ALIGNMENT_AVAILABLE = False
_aligner_ready = threading.Event()


def _load_forced_aligner() -> None:
    """Runs on a daemon thread; never blocks server startup or shutdown."""

    global FORCED_ALIGNMENT_AVAILABLE

    forced_aligner = None

    try:

        from aligner.forced_aligner import forced_aligner as loaded_aligner

        forced_aligner = loaded_aligner
        FORCED_ALIGNMENT_AVAILABLE = True

        logger.info("MMS forced-alignment model loaded — using real per-word timings.")

    except Exception as exc:

        FORCED_ALIGNMENT_AVAILABLE = False

        logger.warning(
            "MMS forced-alignment unavailable (%s). Falling back to "
            "equal-duration timing for all requests.",
            exc
        )

    finally:

        pipeline.set_forced_aligner(forced_aligner, FORCED_ALIGNMENT_AVAILABLE)
        _aligner_ready.set()


threading.Thread(
    target=_load_forced_aligner,
    name="forced-aligner-loader",
    daemon=True,
).start()


# ----------------------------------------------------
# Home Page
# ----------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(
        HTML_DIR,
        "index.html"
    )


# ----------------------------------------------------
# Health / Readiness Probe
#
# Used by the desktop launcher to detect the exact moment this process
# is accepting requests, instead of a fixed startup delay. Deliberately
# does no work beyond reporting already-computed state, so it is cheap
# to poll every ~250ms during startup. Also reports forced-alignment
# availability, which distinguishes "running fully" from "running with
# degraded equal-duration timing" (e.g. while _load_forced_aligner is
# still in progress on a fresh machine).
# ----------------------------------------------------

@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "app": "CBT",
        "forced_alignment": FORCED_ALIGNMENT_AVAILABLE,
        "forced_alignment_ready": _aligner_ready.is_set(),
    })


# ----------------------------------------------------
# Serve CSS
# ----------------------------------------------------

@app.route("/css/<path:filename>")
def css(filename):

    return send_from_directory(
        HTML_DIR / "css",
        filename
    )


# ----------------------------------------------------
# Serve JavaScript
# ----------------------------------------------------

@app.route("/js/<path:filename>")
def js(filename):

    return send_from_directory(
        HTML_DIR / "js",
        filename
    )


# ----------------------------------------------------
# Serve HTML Files
#
# NOTE: this catch-all must stay BELOW /, /healthz, /css/*, /js/*,
# /speak, and /audio/* — Flask matches routes in registration order
# among equally-specific patterns, and a path like /healthz would
# otherwise be swallowed by this handler and returned as a (missing)
# static file instead of the JSON health payload.
# ----------------------------------------------------

@app.route("/<path:filename>")
def html_files(filename):

    return send_from_directory(
        HTML_DIR,
        filename
    )


# ----------------------------------------------------
# Generate Speech
#
# Generates (or serves from cache) the requested voice immediately,
# with no added delay, and kicks off background generation of the
# OTHER voice so that switching voices later is an instant cache hit.
# See speech_pipeline.py for the full flow.
# ----------------------------------------------------

@app.route("/speak", methods=["POST"])
def speak():

    try:

        data = request.get_json()

        original_text = data.get("text", "").strip()

        voice = data.get("voice", "male").strip().lower()
        speed = float(data.get("speed", 1.0))

        # Monotonically increasing per browser session (api.js). A higher
        # epoch means the user has navigated since the older requests were
        # issued, so their generations are abandoned — see cancellation.py.
        # Defaults to 0 for any client that doesn't send one, which simply
        # means "never supersedes anything", preserving the old behaviour.
        try:
            nav_epoch = int(data.get("nav_epoch", 0))
        except (TypeError, ValueError):
            nav_epoch = 0

        logger.info(
            "Speak request: voice=%s speed=%s nav_epoch=%s", voice, speed, nav_epoch
        )

        if not original_text:

            return jsonify({

                "success": False,

                "message": "No text received."

            }), 400

        if voice not in VOICES:

            return jsonify({

                "success": False,

                "message": f"Unknown voice '{voice}'. Expected one of {VOICES}."

            }), 400

        # Registering this epoch cancels every still-running generation
        # from an EARLIER one, killing their Piper subprocess outright —
        # that is what frees the CPU for the page the user is actually
        # on, instead of making it queue behind pages they've left.
        token = cancellation_registry.begin(
            nav_epoch, label=f"{voice}/{original_text[:32]!r}"
        )

        try:
            result = pipeline.handle_request(
                original_text, voice, speed, cancel_token=token
            )
        finally:
            cancellation_registry.end(token)

        return jsonify({

            "success": True,

            "audio": result["audio"],

            "timing": result["timing"],

            # Embedded directly so the client can skip the follow-up GET
            # to `timing` entirely on the common path — see
            # speech_pipeline.py's _response_for.
            "timings": result["timings"],

            "voice": result["voice"],

            "cached": result["cached"]

        })

    except GenerationCancelled as cancelled:

        # Not an error: a newer navigation deliberately abandoned this
        # work. The client that issued it has already aborted its own
        # fetch and will never read this, so the status code only matters
        # for logs/proxies — 409 Conflict ("superseded by a newer
        # request") rather than a 5xx that would look like a real fault.
        logger.info("/speak cancelled: %s", cancelled)

        return jsonify({

            "success": False,

            "cancelled": True,

            "message": "Superseded by a newer request."

        }), 409

    except Exception as e:

        logger.exception("/speak failed")

        return jsonify({

            "success": False,

            "message": str(e)

        }), 500


# ----------------------------------------------------
# Serve Generated / Cached Audio & Timing Files
#
# Cached entries (output/alignment_cache/<key>.wav|.json) and
# ephemeral fallback entries (output/ephemeral/<id>.wav|.json) both
# live under OUTPUT_DIR, so this single generic route serves all of
# them — no per-feature routes needed.
# ----------------------------------------------------

@app.route("/audio/<path:filename>")
def audio(filename):

    return send_from_directory(

        OUTPUT_DIR,

        filename

    )


# ----------------------------------------------------

if __name__ == "__main__":

    # The desktop launcher (launcher/server.py) exports these three
    # environment variables; the defaults below preserve the previous
    # standalone `python host.py` behaviour for local development.
    debug = os.environ.get("CBT_DEBUG", "1") == "1"
    bind_host = os.environ.get("CBT_HOST", "127.0.0.1")
    bind_port = int(os.environ.get("CBT_PORT", "5000"))

    logger.info("Starting HTTP host on %s:%s (debug=%s)", bind_host, bind_port, debug)

    # threaded=True matters here: the /speak handler returns as soon as
    # the requested voice is ready and hands the other voice off to a
    # background thread, but a second incoming request (e.g. the user
    # switching voices immediately) still needs the Flask server itself
    # to be able to accept it concurrently rather than queuing behind
    # the first request's handler.
    #
    # use_reloader is pinned to False even when debug=True: the
    # reloader forks a second process, which would load the
    # forced-alignment model twice (once per process) and double an
    # already expensive startup — and would also start two background
    # loader threads racing to set the same pipeline state.
    app.run(

        host=bind_host,

        port=bind_port,

        debug=debug,

        use_reloader=False,

        threaded=True

    )
