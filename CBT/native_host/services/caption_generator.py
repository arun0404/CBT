"""
Offline WebVTT subtitle generator for locally authored video assets.

Not part of the shipped runtime: host.py never imports this module, and
CBT/native_host/requirements.txt (the shipped app's venv) is untouched by
it. This is a developer-run tool — point it at a video/audio file, get a
.vtt file, and drop that .vtt beside the video under CBT/html/assets/ so
the custom video player's CC button (see index.html's
initVideoSettingsMenu / initCustomVideoPlayers) can load it via a <track>
element already wired up on the <video> in data2.json.

Offline model loading
----------------------
faster-whisper (https://github.com/SYSTRAN/faster-whisper) downloads its
model from Hugging Face Hub on first use unless told not to — which would
break this project's air-gapped design the same way an un-provisioned
TORCH_HOME would for the forced-alignment model (see
launcher/torch_assets.py, which solves the identical problem for the MMS_FA
checkpoint by pointing torch at a pre-provisioned local directory instead
of letting it phone home). Two ways to keep this tool offline too:

  1. On a machine WITH network access, pre-download a model once (e.g. via
     faster-whisper's own huggingface_hub-based download, or
     `huggingface-cli download Systran/faster-whisper-base`) into a local
     folder, then always point this script at that folder directly:

         python caption_generator.py video.mp4 --model-dir C:\\models\\faster-whisper-base

     This never touches the network and is the right mode for an
     air-gapped machine.

  2. Otherwise, --model behaves like faster-whisper's own model name
     (tiny, base, small, ...) and is resolved from whatever is already in
     the local Hugging Face cache. local_files_only is True by default,
     so a machine that has never fetched that model fails loudly instead
     of silently reaching out to the network — pass --allow-network to
     opt into that fetch explicitly.

Requirements (a separate, dev-only environment — NOT
CBT/native_host/requirements.txt):
    pip install -r services/requirements-captions.txt

Usage (run from CBT/native_host/services/ — two levels up to CBT, then
into html/assets):
    python caption_generator.py "..\\..\\html\\assets\\Sample Video.mp4" \\
        --output "..\\..\\html\\assets\\Sample Video.vtt" \\
        --model-dir C:\\models\\faster-whisper-base \\
        --language en
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


def format_timestamp(seconds: float) -> str:
    """Seconds -> WebVTT timestamp: HH:MM:SS.mmm."""

    if seconds < 0:
        seconds = 0.0

    total_ms = round(seconds * 1000)
    hours, remainder_ms = divmod(total_ms, 3_600_000)
    minutes, remainder_ms = divmod(remainder_ms, 60_000)
    secs, millis = divmod(remainder_ms, 1000)

    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def segments_to_vtt(segments) -> str:
    """
    faster-whisper Segment objects -> a WebVTT document string.

    One cue per segment — faster-whisper already splits audio into
    speech-boundary-aware chunks, so no further sentence-splitting is
    applied here.
    """

    lines = ["WEBVTT", ""]

    for segment in segments:

        text = segment.text.strip()

        if not text:
            continue

        lines.append(f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}")
        lines.append(text)
        lines.append("")

    return "\n".join(lines) + "\n"


def generate_vtt(
    input_path: Path,
    output_path: Path,
    model_size_or_path: str,
    language: Optional[str],
    device: str,
    compute_type: str,
    local_files_only: bool,
) -> None:

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SystemExit(
            "faster-whisper is not installed in this environment.\n"
            "Install it with: pip install -r services/requirements-captions.txt\n"
            "(This is a separate, dev-only environment — it is NOT added to "
            "CBT/native_host/requirements.txt, which is the shipped app's venv.)"
        ) from exc

    if not input_path.is_file():
        raise SystemExit(f"Input media not found: {input_path}")

    print(f"Loading model '{model_size_or_path}' (device={device}, compute_type={compute_type})...")

    model = WhisperModel(
        model_size_or_path,
        device=device,
        compute_type=compute_type,
        local_files_only=local_files_only,
    )

    print(f"Transcribing {input_path.name} ...")

    segments, info = model.transcribe(str(input_path), language=language, beam_size=5)

    print(f"Detected language: {info.language} (p={info.language_probability:.2f})")

    # transcribe() returns a lazy generator — materialize it once so both
    # the VTT writer and the segment count printed below see the same list.
    segments = list(segments)

    vtt_text = segments_to_vtt(segments)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(vtt_text, encoding="utf-8")

    print(f"Wrote {len(segments)} cue(s) to {output_path}")


def main(argv: Optional[list] = None) -> int:

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("input", type=Path, help="Path to the local video/audio file to transcribe.")
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output .vtt path (default: input file with a .vtt extension).",
    )
    parser.add_argument(
        "--model", default="base",
        help="Model size (tiny, base, small, ...) resolved from the local Hugging Face cache. "
             "Ignored if --model-dir is given.",
    )
    parser.add_argument(
        "--model-dir", type=Path, default=None,
        help="Path to a local, pre-downloaded CTranslate2 Whisper model directory. Takes priority "
             "over --model and never touches the network.",
    )
    parser.add_argument(
        "--language", default=None,
        help="Force a language code (e.g. 'en'). Default: auto-detect.",
    )
    parser.add_argument(
        "--device", default="cpu", choices=["cpu", "cuda", "auto"],
        help="Inference device (default: cpu, matching this project's offline/no-GPU-assumed "
             "deployment target).",
    )
    parser.add_argument(
        "--compute-type", default="int8",
        help="ctranslate2 compute type (default: int8 — fastest on CPU).",
    )
    parser.add_argument(
        "--allow-network", action="store_true",
        help="Allow faster-whisper to fetch the model from Hugging Face Hub if it isn't cached "
             "locally. Off by default to match this app's air-gapped design.",
    )

    args = parser.parse_args(argv)

    output_path = args.output or args.input.with_suffix(".vtt")
    model_ref = str(args.model_dir) if args.model_dir else args.model

    generate_vtt(
        input_path=args.input,
        output_path=output_path,
        model_size_or_path=model_ref,
        language=args.language,
        device=args.device,
        compute_type=args.compute_type,
        local_files_only=not args.allow_network,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
