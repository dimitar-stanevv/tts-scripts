#!/usr/bin/env python3
"""Unified ElevenLabs TTS + FLAC Converter
=========================================
Reads a semicolon-delimited CSV with the following structure:

  filename  ; description ; <lang1> ; <lang2> ; ...
  alert_foo ; Some note   ; Hello   ; Hola    ; ...

For every non-empty (filename, language) cell the script:
  1. Calls the ElevenLabs API and retrieves MP3 audio (in-memory).
  2. Pipes the MP3 bytes directly into ffmpeg to produce a FLAC file.

No intermediate MP3 files are written to disk.

Output filenames: {filename}_{lang}.flac  (e.g. alert_avgspeed_en.flac)

Usage:
  pip install requests
  Copy local_config.example.ini to local_config.ini and set api_key (gitignored), or use --api-key / ELEVENLABS_API_KEY.
  python generate_tts.py --csv tts_messages_new.csv --out ./output
"""
from __future__ import annotations

import argparse
import configparser
import csv
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "local_config.ini"

# ── ElevenLabs config ────────────────────────────────────────────────────────
API_BASE         = "https://api.elevenlabs.io/v1"
MODEL_ID         = "eleven_multilingual_v2"
OUTPUT_FORMAT    = "mp3_44100_128"       # intermediate format before FLAC conversion
REQUESTS_PER_SEC = 2                     # rate-limit guard

DEFAULT_SPEED    = 1.05

VOICE_SETTINGS = {
    "stability":         1.0,
    "similarity_boost":  0.75,
    "style":             0.0,
    "use_speaker_boost": True,
}

# ── Voice IDs ─────────────────────────────────────────────────────────────────
# Add per-language overrides here as needed.
# Any language not listed falls back to DEFAULT_VOICE_ID.
DEFAULT_VOICE_ID = "WZlYpi1yf6zJhNWXih74"

VOICE_OVERRIDES: dict[str, str] = {
    "bg": "M1ydWt7KnBCiuv4CnEDC",
}

# ── Reserved CSV column names (not language codes) ───────────────────────────
RESERVED_COLUMNS = {"filename", "description"}


def load_api_key_from_config(config_path: Path) -> str | None:
    if not config_path.is_file():
        return None
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    key = parser.get("elevenlabs", "api_key", fallback="").strip()
    if not key or key.startswith("YOUR_"):
        return None
    return key


def resolve_api_key(cli_key: str | None, config_path: Path) -> str | None:
    if cli_key and cli_key.strip():
        return cli_key.strip()
    env_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if env_key:
        return env_key
    return load_api_key_from_config(config_path)


# ── ElevenLabs synthesis ──────────────────────────────────────────────────────
def synthesize(text: str, voice_id: str, api_key: str, speed: float) -> bytes:
    url = f"{API_BASE}/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key":   api_key,
        "Content-Type": "application/json",
        "Accept":       "audio/mpeg",
    }
    payload = {
        "text":           text,
        "model_id":       MODEL_ID,
        "voice_settings": VOICE_SETTINGS,
        "speed":          speed,
    }
    params = {"output_format": OUTPUT_FORMAT}

    response = requests.post(url, json=payload, headers=headers, params=params, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    return response.content


# ── MP3 bytes → FLAC file via ffmpeg ─────────────────────────────────────────
def mp3_bytes_to_flac(mp3_bytes: bytes, out_path: str, compression: str) -> None:
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",          # read MP3 from stdin
        "-vn",
        "-c:a", "flac",
        "-compression_level", compression,
        "-n",                    # never overwrite
        out_path,
    ]
    result = subprocess.run(cmd, input=mp3_bytes, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip().splitlines()[-1])


# ── Main batch processor ──────────────────────────────────────────────────────
def process_csv(csv_path: str, out_dir: str, api_key: str, compression: str,
                speed: float, dry_run: bool) -> None:
    os.makedirs(out_dir, exist_ok=True)

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)

    if not rows:
        print("CSV is empty. Nothing to do.")
        return

    fieldnames = list(rows[0].keys())
    lang_columns = [c for c in fieldnames if c not in RESERVED_COLUMNS]

    if not lang_columns:
        sys.exit("ERROR: No language columns found in CSV (expected columns other than 'filename' and 'description').")

    total_jobs = sum(
        1 for row in rows for lang in lang_columns if row.get(lang, "").strip()
    )

    print(f"Messages  : {len(rows)}")
    print(f"Languages : {lang_columns}")
    print(f"Total jobs: {total_jobs}")
    print(f"Output dir: {out_dir}")
    if dry_run:
        print("DRY RUN — no API calls or ffmpeg conversions will be made.")
    print()

    delay  = 1.0 / REQUESTS_PER_SEC
    done   = 0
    skipped = 0
    errors = 0

    for row in rows:
        stem = row.get("filename", "").strip()
        if not stem:
            print("  [SKIP] Row with empty filename — skipping entire row")
            skipped += len(lang_columns)
            continue

        for lang in lang_columns:
            text = row.get(lang, "").strip()
            if not text:
                print(f"  [SKIP] {stem}_{lang}: empty text")
                skipped += 1
                continue

            voice_id = VOICE_OVERRIDES.get(lang, DEFAULT_VOICE_ID)
            out_name = f"{stem}_{lang}.flac"
            out_path = os.path.join(out_dir, out_name)

            if os.path.exists(out_path):
                print(f"  [EXIST] {out_name} — already exists, skipping")
                skipped += 1
                continue

            if dry_run:
                print(
                    f"  [DRY]  {out_name}"
                    f"  |  voice={voice_id}"
                    f"  |  speed={speed}"
                    f"  |  text={text[:60]!r}"
                )
                done += 1
                continue

            try:
                print(f"  [GEN]  {out_name} ...", end=" ", flush=True)
                mp3_bytes = synthesize(text, voice_id, api_key, speed)
                mp3_bytes_to_flac(mp3_bytes, out_path, compression)
                size_kb = os.path.getsize(out_path) / 1024
                print(f"✓  ({size_kb:.1f} KB)")
                done += 1
            except Exception as exc:
                print(f"✗  ERROR: {exc}")
                errors += 1

            time.sleep(delay)

    print()
    print("─" * 40)
    print(f"Done:    {done}")
    print(f"Skipped: {skipped}")
    print(f"Errors:  {errors}")
    if errors:
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="ElevenLabs batch TTS → FLAC from a multilingual CSV file."
    )
    parser.add_argument(
        "--csv", required=True,
        help="Path to input CSV file",
    )
    parser.add_argument(
        "--out", default="./tts_output",
        help="Output directory (default: ./tts_output)",
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"INI file with [elevenlabs] api_key (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--api-key", default=None,
        help="ElevenLabs API key (overrides ELEVENLABS_API_KEY and --config file)",
    )
    parser.add_argument(
        "--compression", default="5", choices=[str(i) for i in range(13)],
        help="FLAC compression level 0-12 (default: 5)",
    )
    parser.add_argument(
        "--speed", type=float, default=DEFAULT_SPEED,
        help=f"TTS speed multiplier (default: {DEFAULT_SPEED})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be generated without calling the API or ffmpeg",
    )
    args = parser.parse_args()

    api_key = resolve_api_key(args.api_key, args.config)
    if not args.dry_run and not api_key:
        sys.exit(
            "ERROR: Set the API key via --api-key, ELEVENLABS_API_KEY, or "
            f"[elevenlabs] api_key in {args.config} (see local_config.example.ini)."
        )

    process_csv(args.csv, args.out, api_key, args.compression, args.speed, args.dry_run)


if __name__ == "__main__":
    main()
