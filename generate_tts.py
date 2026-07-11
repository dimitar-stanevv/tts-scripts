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

# ── Fallback defaults ────────────────────────────────────────────────────────
# These are used only when the CSV does not supply a value for a given language
# (no matching "@" config row, or an empty cell). Per-language values in the CSV
# always take precedence.
DEFAULT_VOICE_ID   = "WZlYpi1yf6zJhNWXih74"
DEFAULT_MODEL_ID   = MODEL_ID
DEFAULT_SPEED      = 1.05
TAIL_TRIM_SECONDS  = 0.25   # seconds to trim from the end of every generated file

DEFAULT_VOICE_SETTINGS = {
    "stability":         1.0,
    "similarity_boost":  0.75,
    "style":             0.0,
    "use_speaker_boost": True,
}

# Legacy per-language voice overrides. The CSV "@voice_id" row supersedes this;
# kept only as a fallback for CSVs that predate per-language config rows.
VOICE_OVERRIDES: dict[str, str] = {
    "bg": "M1ydWt7KnBCiuv4CnEDC",
}

# ── CSV column / row conventions ─────────────────────────────────────────────
# Columns that are not language codes.
RESERVED_COLUMNS = {"filename", "description"}

# Rows whose "filename" begins with this marker are not messages: they hold
# per-language synthesis settings, one value per language column. A leading "__"
# is used (rather than e.g. "@" or "=") so spreadsheet apps don't treat the cell
# as a formula when the CSV is hand-edited.
CONFIG_PREFIX = "__"

# Recognised config-row keys (the part after CONFIG_PREFIX, lower-cased).
CONFIG_KEYS = {
    "voice_id",
    "model_id",
    "speed",
    "stability",
    "similarity_boost",
    "style",
    "use_speaker_boost",
}


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
def synthesize(text: str, settings: dict, api_key: str) -> bytes:
    url = f"{API_BASE}/text-to-speech/{settings['voice_id']}"
    headers = {
        "xi-api-key":   api_key,
        "Content-Type": "application/json",
        "Accept":       "audio/mpeg",
    }
    payload = {
        "text":           text,
        "model_id":       settings["model_id"],
        "voice_settings": settings["voice_settings"],
        "speed":          settings["speed"],
    }
    params = {"output_format": OUTPUT_FORMAT}

    response = requests.post(url, json=payload, headers=headers, params=params, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    return response.content


# ── MP3 bytes → FLAC file via ffmpeg ─────────────────────────────────────────
def mp3_bytes_to_flac(mp3_bytes: bytes, out_path: str, compression: str,
                      trim_end: float = 0.0) -> None:
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",          # read MP3 from stdin
        "-vn",
        "-c:a", "flac",
        "-compression_level", compression,
    ]
    if trim_end > 0:
        # Reverse → trim from the new "start" (original tail) → reverse back.
        # Works without knowing the file duration upfront.
        cmd += ["-af", f"areverse,atrim=start={trim_end},areverse"]
    cmd += [
        "-n",                    # never overwrite
        out_path,
    ]
    result = subprocess.run(cmd, input=mp3_bytes, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip().splitlines()[-1])


# ── Per-language config parsing ──────────────────────────────────────────────
def _parse_float(raw: str, default: float) -> float:
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"  [WARN] Could not parse {raw!r} as a number; using {default}")
        return default


def _parse_bool(raw: str, default: bool) -> bool:
    raw = (raw or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    print(f"  [WARN] Could not parse {raw!r} as a boolean; using {default}")
    return default


def split_config_rows(rows: list[dict], lang_columns: list[str]):
    """Separate "@"-prefixed config rows from message rows.

    Returns (config, message_rows), where config maps a config key
    (e.g. "voice_id") to a {lang: raw_cell_value} dict.
    """
    config: dict[str, dict[str, str]] = {}
    message_rows: list[dict] = []
    for row in rows:
        stem = (row.get("filename") or "").strip()
        if stem.startswith(CONFIG_PREFIX):
            key = stem[len(CONFIG_PREFIX):].strip().lower()
            if key not in CONFIG_KEYS:
                print(f"  [WARN] Unknown config row {stem!r} — ignoring "
                      f"(known: {', '.join(CONFIG_PREFIX + k for k in sorted(CONFIG_KEYS))})")
                continue
            config[key] = {lang: row.get(lang, "") for lang in lang_columns}
        else:
            message_rows.append(row)
    return config, message_rows


def build_lang_settings(lang_columns: list[str], config: dict,
                        speed_override: float | None) -> dict[str, dict]:
    """Resolve the effective synthesis settings for every language column.

    Precedence per value: CSV "@" config cell → legacy fallback → script default.
    A non-None speed_override (from --speed) wins over everything for speed.
    """
    settings: dict[str, dict] = {}
    for lang in lang_columns:
        voice_id = (config.get("voice_id", {}).get(lang, "").strip()
                    or VOICE_OVERRIDES.get(lang)
                    or DEFAULT_VOICE_ID)
        model_id = config.get("model_id", {}).get(lang, "").strip() or DEFAULT_MODEL_ID
        speed = _parse_float(config.get("speed", {}).get(lang, ""), DEFAULT_SPEED)
        if speed_override is not None:
            speed = speed_override
        voice_settings = {
            "stability":         _parse_float(config.get("stability", {}).get(lang, ""),        DEFAULT_VOICE_SETTINGS["stability"]),
            "similarity_boost":  _parse_float(config.get("similarity_boost", {}).get(lang, ""), DEFAULT_VOICE_SETTINGS["similarity_boost"]),
            "style":             _parse_float(config.get("style", {}).get(lang, ""),            DEFAULT_VOICE_SETTINGS["style"]),
            "use_speaker_boost": _parse_bool(config.get("use_speaker_boost", {}).get(lang, ""), DEFAULT_VOICE_SETTINGS["use_speaker_boost"]),
        }
        settings[lang] = {
            "voice_id":       voice_id,
            "model_id":       model_id,
            "speed":          speed,
            "voice_settings": voice_settings,
        }
    return settings


# ── Main batch processor ──────────────────────────────────────────────────────
def process_csv(csv_path: str, out_dir: str, api_key: str, compression: str,
                speed_override: float | None, dry_run: bool, trim_end: float = 0.0) -> None:
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

    config, message_rows = split_config_rows(rows, lang_columns)
    lang_settings = build_lang_settings(lang_columns, config, speed_override)

    total_jobs = sum(
        1 for row in message_rows for lang in lang_columns if row.get(lang, "").strip()
    )

    print(f"Messages  : {len(message_rows)}")
    print(f"Languages : {lang_columns}")
    print(f"Total jobs: {total_jobs}")
    print(f"Output dir: {out_dir}")
    print("Per-language settings:")
    for lang in lang_columns:
        s = lang_settings[lang]
        vs = s["voice_settings"]
        print(f"  {lang:<3} voice={s['voice_id']}  model={s['model_id']}  "
              f"speed={s['speed']}  stab={vs['stability']}  sim={vs['similarity_boost']}  "
              f"style={vs['style']}  spk_boost={vs['use_speaker_boost']}")
    if dry_run:
        print("DRY RUN — no API calls or ffmpeg conversions will be made.")
    print()

    delay  = 1.0 / REQUESTS_PER_SEC
    done   = 0
    skipped = 0
    errors = 0

    for row in message_rows:
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

            settings = lang_settings[lang]
            out_name = f"{stem}_{lang}.flac"
            out_path = os.path.join(out_dir, out_name)

            if os.path.exists(out_path):
                print(f"  [EXIST] {out_name} — already exists, skipping")
                skipped += 1
                continue

            if dry_run:
                print(
                    f"  [DRY]  {out_name}"
                    f"  |  voice={settings['voice_id']}"
                    f"  |  model={settings['model_id']}"
                    f"  |  speed={settings['speed']}"
                    f"  |  {settings['voice_settings']}"
                    f"  |  text={text[:50]!r}"
                )
                done += 1
                continue

            try:
                print(f"  [GEN]  {out_name} ...", end=" ", flush=True)
                mp3_bytes = synthesize(text, settings, api_key)
                mp3_bytes_to_flac(mp3_bytes, out_path, compression, trim_end)
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
        "--speed", type=float, default=None,
        help="Override TTS speed for ALL languages, ignoring the per-language "
             f"'@speed' values in the CSV (CSV/script default: {DEFAULT_SPEED}).",
    )
    parser.add_argument(
        "--trim-end", type=float, default=TAIL_TRIM_SECONDS,
        metavar="SECONDS",
        help=f"Seconds to trim from the end of each generated file (default: {TAIL_TRIM_SECONDS})",
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

    process_csv(args.csv, args.out, api_key, args.compression, args.speed, args.dry_run,
                args.trim_end)


if __name__ == "__main__":
    main()
