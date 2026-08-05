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
import re
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
# No tail trim by default. A fixed trim is unreliable: ElevenLabs leaves a
# variable amount of trailing silence, so a fixed cut chopped the final word on
# every file that had less silence than the trim. Leaving endings intact sounds
# better than clipping words. (Still overridable via --trim-end for one-offs.)
TAIL_TRIM_SECONDS  = 0.0

# Loudness normalization: raw ElevenLabs output varies a lot per voice/language
# (measured -16.6 to -22.1 LUFS across languages), so each file is measured and
# then gained to the same integrated-loudness target. The limiter only catches
# peaks that would clip after the gain.
TARGET_LUFS     = -14.0   # EBU R128 integrated loudness target
PEAK_CEILING_DB = -1.0    # limiter ceiling applied after the gain

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
        # "speed" must live inside voice_settings; as a top-level field the API
        # silently ignores it and renders at speed 1.0.
        "voice_settings": {**settings["voice_settings"], "speed": settings["speed"]},
    }
    params = {"output_format": OUTPUT_FORMAT}

    response = requests.post(url, json=payload, headers=headers, params=params, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    return response.content


# ── Loudness measurement (pass 1 of 2) ───────────────────────────────────────
def measure_integrated_lufs(mp3_bytes: bytes | None = None,
                            path: str | None = None) -> float | None:
    """Measure EBU R128 integrated loudness of MP3 bytes (stdin) or a file.

    Returns None when no loudness can be measured (e.g. silent audio).
    """
    source = ["-i", "pipe:0"] if mp3_bytes is not None else ["-i", str(path)]
    # ebur128 prints its summary at the default (info) loglevel, so no
    # "-loglevel error" here.
    cmd = ["ffmpeg", *source, "-af", "ebur128", "-f", "null", "-"]
    result = subprocess.run(cmd, input=mp3_bytes, capture_output=True)
    stderr = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        raise RuntimeError(stderr.strip().splitlines()[-1])
    match = re.search(r"^\s*I:\s*(-?[0-9.]+)\s*LUFS", stderr, re.MULTILINE)
    return float(match.group(1)) if match else None


def loudness_filters(gain_db: float) -> list[str]:
    """Gain to the loudness target, then a limiter so new peaks cannot clip."""
    ceiling_linear = 10 ** (PEAK_CEILING_DB / 20)
    return [
        f"volume={gain_db:+.2f}dB",
        f"alimiter=limit={ceiling_linear:.6f}:level=false",
    ]


# ── MP3 bytes → FLAC file via ffmpeg (pass 2 of 2) ───────────────────────────
def mp3_bytes_to_flac(mp3_bytes: bytes, out_path: str, compression: str,
                      trim_end: float = 0.0, gain_db: float | None = None) -> None:
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",          # read MP3 from stdin
        "-vn",
        "-c:a", "flac",
        "-compression_level", compression,
    ]
    filters = []
    if trim_end > 0:
        # Reverse → trim from the new "start" (original tail) → reverse back.
        # Works without knowing the file duration upfront.
        filters.append(f"areverse,atrim=start={trim_end},areverse")
    if gain_db is not None:
        filters += loudness_filters(gain_db)
    if filters:
        cmd += ["-af", ",".join(filters)]
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
                speed_override: float | None, dry_run: bool, trim_end: float = 0.0,
                target_lufs: float | None = TARGET_LUFS) -> None:
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
    if target_lufs is not None:
        print(f"Loudness  : normalize to {target_lufs} LUFS (peak ceiling {PEAK_CEILING_DB} dB)")
    else:
        print("Loudness  : normalization disabled")
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
                gain_db = None
                if target_lufs is not None:
                    measured = measure_integrated_lufs(mp3_bytes)
                    if measured is None:
                        print("[WARN: loudness unmeasurable, gain skipped]", end=" ")
                    else:
                        gain_db = target_lufs - measured
                mp3_bytes_to_flac(mp3_bytes, out_path, compression, trim_end, gain_db)
                size_kb = os.path.getsize(out_path) / 1024
                gain_note = f", gain {gain_db:+.1f} dB" if gain_db is not None else ""
                print(f"✓  ({size_kb:.1f} KB{gain_note})")
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


# ── Normalize already-generated files (no API calls) ─────────────────────────
def normalize_existing_flacs(out_dir: str, compression: str, target_lufs: float) -> None:
    """Loudness-normalize every .flac in out_dir in place, without re-synthesis.

    Idempotent: files within 1 dB of the target are left untouched. The
    tolerance is deliberately that wide because a file whose gain was partly
    absorbed by the limiter lands slightly under target; re-processing it
    every run would just squash its peaks further without getting louder.
    """
    flac_files = sorted(Path(out_dir).glob("*.flac"))
    if not flac_files:
        print(f"No .flac files found in {out_dir}. Nothing to do.")
        return

    print(f"Files     : {len(flac_files)}")
    print(f"Loudness  : normalize to {target_lufs} LUFS (peak ceiling {PEAK_CEILING_DB} dB)")
    print()

    done = 0
    skipped = 0
    errors = 0
    for flac_path in flac_files:
        try:
            print(f"  [NORM] {flac_path.name} ...", end=" ", flush=True)
            measured = measure_integrated_lufs(path=str(flac_path))
            if measured is None:
                print("skipped (loudness unmeasurable)")
                skipped += 1
                continue
            gain_db = target_lufs - measured
            if abs(gain_db) < 1.0:
                print(f"already at {measured:.1f} LUFS, skipping")
                skipped += 1
                continue
            tmp_path = flac_path.with_name(flac_path.stem + ".norm-tmp.flac")
            cmd = [
                "ffmpeg",
                "-loglevel", "error",
                "-i", str(flac_path),
                "-vn",
                "-c:a", "flac",
                "-compression_level", compression,
                "-af", ",".join(loudness_filters(gain_db)),
                "-y", str(tmp_path),
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError(result.stderr.decode(errors="replace").strip().splitlines()[-1])
            os.replace(tmp_path, flac_path)
            print(f"✓  ({measured:.1f} LUFS, gain {gain_db:+.1f} dB)")
            done += 1
        except Exception as exc:
            print(f"✗  ERROR: {exc}")
            errors += 1

    print()
    print("─" * 40)
    print(f"Normalized: {done}")
    print(f"Skipped:    {skipped}")
    print(f"Errors:     {errors}")
    if errors:
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="ElevenLabs batch TTS → FLAC from a multilingual CSV file."
    )
    parser.add_argument(
        "--csv",
        help="Path to input CSV file (required unless --normalize-existing)",
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
        "--lufs", type=float, default=TARGET_LUFS,
        help="Loudness target in LUFS; every file is gained to this integrated "
             f"loudness, with a {PEAK_CEILING_DB} dB peak limiter to prevent clipping. "
             f"Less negative = louder (default: {TARGET_LUFS}).",
    )
    parser.add_argument(
        "--no-normalize", action="store_true",
        help="Disable loudness normalization (keep raw ElevenLabs levels)",
    )
    parser.add_argument(
        "--normalize-existing", action="store_true",
        help="Skip generation entirely; loudness-normalize all existing .flac "
             "files in --out in place (no API key needed, idempotent)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be generated without calling the API or ffmpeg",
    )
    args = parser.parse_args()

    target_lufs = None if args.no_normalize else args.lufs

    if args.normalize_existing:
        if target_lufs is None:
            sys.exit("ERROR: --normalize-existing and --no-normalize are contradictory.")
        normalize_existing_flacs(args.out, args.compression, target_lufs)
        return

    if not args.csv:
        sys.exit("ERROR: --csv is required (unless using --normalize-existing).")

    api_key = resolve_api_key(args.api_key, args.config)
    if not args.dry_run and not api_key:
        sys.exit(
            "ERROR: Set the API key via --api-key, ELEVENLABS_API_KEY, or "
            f"[elevenlabs] api_key in {args.config} (see local_config.example.ini)."
        )

    process_csv(args.csv, args.out, api_key, args.compression, args.speed, args.dry_run,
                args.trim_end, target_lufs)


if __name__ == "__main__":
    main()
