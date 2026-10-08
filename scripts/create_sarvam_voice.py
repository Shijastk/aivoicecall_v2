#!/usr/bin/env python3
"""Create a reusable Sarvam cloned voice from one local reference clip.

The script never reads or writes .env and never prints the API key or the
provider-generated transcript. It prints only the saved voice ID needed by
SHUO after a successful create.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
from pathlib import Path

import httpx


CREATE_VOICE_URL = "https://api.sarvam.ai/voices/create"
MAX_FILE_BYTES = 50 * 1024 * 1024
STYLE_CHOICES = (
    "conversational",
    "audiobooks",
    "entertainment",
    "sales",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a clean single-speaker reference clip and create a "
            "reusable Sarvam saved voice."
        )
    )
    parser.add_argument("audio", type=Path)
    parser.add_argument(
        "--name",
        help="Display name. Defaults to the audio filename stem.",
    )
    parser.add_argument(
        "--language",
        default="ml-IN",
        help="BCP-47 language of the reference clip (default: ml-IN).",
    )
    parser.add_argument(
        "--style",
        choices=STYLE_CHOICES,
        default="conversational",
    )
    parser.add_argument(
        "--gender",
        choices=("male", "female"),
        default=None,
    )
    parser.add_argument(
        "--confirm-rights",
        action="store_true",
        help="Confirm that you have the right and consent to clone this voice.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.confirm_rights:
        raise SystemExit(
            "STOP: pass --confirm-rights only when you have the right "
            "and consent to clone this voice"
        )

    audio = args.audio.expanduser().resolve()
    if not audio.is_file():
        raise SystemExit(f"STOP: audio file does not exist: {audio}")

    size = audio.stat().st_size
    if size <= 0:
        raise SystemExit("STOP: audio file is empty")
    if size > MAX_FILE_BYTES:
        raise SystemExit("STOP: Sarvam create-voice file limit is 50 MB")

    api_key = (os.getenv("SARVAM_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("STOP: SARVAM_API_KEY is not set")

    name = (args.name or audio.stem).strip()
    if not 1 <= len(name) <= 100:
        raise SystemExit("STOP: --name must contain 1-100 characters")

    content_type = (
        mimetypes.guess_type(audio.name)[0]
        or "application/octet-stream"
    )
    data = {
        "name": name,
        "language": args.language,
        "style": args.style,
    }
    if args.gender:
        data["gender"] = args.gender

    with audio.open("rb") as handle, httpx.Client(timeout=120.0) as client:
        response = client.post(
            CREATE_VOICE_URL,
            headers={"api-subscription-key": api_key},
            data=data,
            files={
                "file": (
                    audio.name,
                    handle,
                    content_type,
                )
            },
        )

    if response.status_code != 201:
        message = ""
        try:
            payload = response.json()
            message = (
                payload.get("message")
                or payload.get("detail")
                or payload.get("error")
                or ""
            )
        except Exception:
            pass
        suffix = f": {message}" if message else ""
        raise SystemExit(
            f"STOP: Sarvam create voice returned "
            f"HTTP {response.status_code}{suffix}"
        )

    try:
        voice_id = response.json()["data"]["voice_id"]
    except Exception as exc:
        raise SystemExit(
            "STOP: Sarvam response did not contain data.voice_id"
        ) from exc

    print(f"SARVAM_VOICE_ID={voice_id}")


if __name__ == "__main__":
    main()
