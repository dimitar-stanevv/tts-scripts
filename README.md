# Unified TTS + FLAC Generator

Batch-generate multilingual FLAC audio files from a CSV using the [ElevenLabs](https://elevenlabs.io) API. Each message is synthesised to MP3 in memory and immediately converted to FLAC via ffmpeg — no intermediate files are written to disk.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.9+ | Uses `from __future__ import annotations` for modern type syntax |
| [requests](https://pypi.org/project/requests/) | HTTP client for the ElevenLabs API |
| [ffmpeg](https://ffmpeg.org/) | Audio conversion, must be on your `PATH` |

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Install ffmpeg:

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg
```

---

## API key (local config file)

The preferred way to store your ElevenLabs API key locally is **`local_config.ini`** next to `generate_tts.py`. That file is listed in [`.gitignore`](.gitignore) so it is not committed.

1. Copy the template and edit the key:

```bash
cp local_config.example.ini local_config.ini
# Edit local_config.ini and set api_key under [elevenlabs]
```

2. Format of `local_config.ini`:

```ini
[elevenlabs]
api_key = sk_your_key_here
```

**Precedence** (first match wins): `--api-key` → `ELEVENLABS_API_KEY` → `[elevenlabs] api_key` in the config file (default path: `local_config.ini` next to `generate_tts.py`, overridable with `--config`).

If you clone the repo on a new machine, create `local_config.ini` yourself (it is not in git). The committed template is [`local_config.example.ini`](local_config.example.ini).

---

## CSV Format

The input CSV is **semicolon-delimited** with the following columns:

| Column | Required | Description |
|--------|----------|-------------|
| `filename` | Yes | Stem of the output file (e.g. `alert_avgspeed`) |
| `description` | No | Human-readable note about when/how the clip is used — ignored during generation |
| `<lang>` | At least one | One column per language code (e.g. `en`, `de`, `bg`). The column header becomes the language suffix in the output filename. |

**Language columns are discovered dynamically** — any column that is not `filename` or `description` is treated as a language. To add a new language, simply add a new column; no script changes are needed.

### Per-language config rows (`__`-prefixed)

Because each language is a **column**, per-language synthesis settings live in special
**rows** whose `filename` starts with `__`. Each cell holds that setting's value for the
language in that column. These rows are read as configuration, not synthesised as audio.

| Config row | Meaning | Default |
|------------|---------|---------|
| `__voice_id` | ElevenLabs voice ID | `WZlYpi1yf6zJhNWXih74` (`bg` → `M1ydWt7KnBCiuv4CnEDC`) |
| `__model_id` | ElevenLabs model ID | `eleven_multilingual_v2` |
| `__speed` | Speech speed multiplier | `1.05` |
| `__stability` | Voice stability (0–1) | `1.0` |
| `__similarity_boost` | Similarity boost (0–1) | `0.75` |
| `__style` | Style exaggeration (0–1) | `0.0` |
| `__use_speaker_boost` | Speaker boost (`true`/`false`) | `true` |

Any config row — or any individual cell within it — that is **missing or empty** falls
back to the built-in script default, so older single-language CSVs without `__` rows still
work unchanged. Unknown `__` rows are warned about and ignored.

### Example

```csv
filename;description;en;de;bg
__voice_id;ElevenLabs voice ID (per language);WZlYpi1yf6zJhNWXih74;WZlYpi1yf6zJhNWXih74;M1ydWt7KnBCiuv4CnEDC
__speed;Speech speed multiplier;1.05;1.05;1.0
__stability;Voice stability 0-1;1.0;1.0;0.9
alert_avgspeed;Exceeding average speed alert;You are exceeding the average speed!;Sie überschreiten die Durchschnittsgeschwindigkeit!;Превишавате средната скорост!
alert_speedcam;Static speed camera danger;Speed camera ahead.;Achtung Blitzer.;Камера за скорост напред.
alert_in;;in;in;в
```

Empty **message** cells are silently skipped — you can leave a cell blank if a translation is not yet available for a given language.

---

## Output

Files are written as:

```
{out_dir}/{filename}_{lang}.flac
```

Examples:

```
tts_output/alert_avgspeed_en.flac
tts_output/alert_avgspeed_de.flac
tts_output/alert_speedcam_bg.flac
```

Existing files are **never overwritten**. Re-running the script is safe and will only generate missing files.

---

## Voice IDs &amp; Settings

Voice IDs and all voice settings are configured **per language in the CSV** via the
`__`-prefixed config rows described above (`__voice_id`, `__model_id`, `__speed`,
`__stability`, `__similarity_boost`, `__style`, `__use_speaker_boost`). To give a language
a more suitable voice or tune its delivery, edit that language's cell in the relevant
`__` row — no code changes needed.

The script still holds the same values as **fallback defaults** (used only when a CSV
cell/row is absent), in `DEFAULT_VOICE_ID`, `DEFAULT_MODEL_ID`, `DEFAULT_SPEED`, and
`DEFAULT_VOICE_SETTINGS` in `generate_tts.py`. The legacy `VOICE_OVERRIDES` dict is kept
only for CSVs that predate the `__voice_id` row; the CSV always wins.

Resolution order for each value: **CSV `__` cell → fallback → script default**.
`--speed` overrides the per-language `__speed` for *all* languages when you need a quick
global tweak.

---

## Usage

```bash
python generate_tts.py --csv tts_messages_all_languages.csv [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--csv PATH` | *(required unless `--normalize-existing`)* | Path to input CSV file |
| `--out DIR` | `./tts_output` | Directory where `.flac` files are saved (created if it does not exist) |
| `--config PATH` | `local_config.ini` next to `generate_tts.py` | INI file with `[elevenlabs] api_key = ...` |
| `--api-key KEY` | *(see API key section)* | Overrides env var and config file. |
| `--compression N` | `5` | FLAC compression level 0–12. Higher values produce smaller files at the cost of encoding time. |
| `--speed N` | *(per-language `__speed` from CSV)* | Override speech speed for **all** languages, ignoring the CSV `__speed` row. |
| `--trim-end SECONDS` | `0` (off) | Seconds trimmed from the end of each generated file. Off by default — ElevenLabs leaves a variable amount of trailing silence, so a fixed cut chopped the final word on any file with less silence than the trim. |
| `--lufs N` | `-14.0` | Loudness target (EBU R128 integrated LUFS). Less negative = louder, e.g. `-12`. |
| `--no-normalize` | off | Disable loudness normalization and keep raw ElevenLabs levels. |
| `--normalize-existing` | off | Skip generation; loudness-normalize all existing `.flac` files in `--out` **in place**. No API key needed, safe to re-run (idempotent). |
| `--dry-run` | off | Preview all jobs without making API calls or running ffmpeg. |

### Loudness normalization

Raw ElevenLabs output varies noticeably per voice/language (measured −16.6 to −22.1 LUFS
across this repo's languages). Every generated file is therefore measured (ffmpeg
`ebur128`) and gained to a common integrated-loudness target (default **−14 LUFS**), with
a **−1 dB peak limiter** so the added gain can never clip. This makes all files both
louder and equally loud across languages. Tune with `--lufs` (e.g. `--lufs -12` for
louder) or turn it off with `--no-normalize`.

Files generated **before** this feature can be brought to the same level without
re-synthesis (no API cost):

```bash
python generate_tts.py --normalize-existing --out ./tts_output
```

---

## Examples

```bash
# Preview everything — no API calls, no ffmpeg
python generate_tts.py --csv tts_messages_all_languages.csv --dry-run

# Generate all audio (reads key from local_config.ini if present)
python generate_tts.py --csv tts_messages_all_languages.csv --out ./output

# Or pass the key explicitly (overrides config and env)
python generate_tts.py \
  --csv tts_messages_all_languages.csv \
  --out ./output \
  --api-key sk-...

# Or use an environment variable (overrides config file only)
export ELEVENLABS_API_KEY="sk-..."
python generate_tts.py --csv tts_messages_all_languages.csv --out ./output

# Maximum FLAC compression, slightly slower speech
python generate_tts.py \
  --csv tts_messages_all_languages.csv \
  --out ./output \
  --compression 12 \
  --speed 1.0

# Make already-generated files louder without re-calling the API
python generate_tts.py --normalize-existing --out ./tts_output

# Louder target for new files
python generate_tts.py --csv tts_messages_all_languages.csv --lufs -12
```

---

## Sample Output

```
Messages  : 27
Languages : ['en', 'de', 'bg']
Total jobs: 63
Output dir: ./output

  [GEN]  alert_avgspeed_en.flac ... ✓  (38.4 KB)
  [GEN]  alert_avgspeed_de.flac ... ✓  (44.1 KB)
  [GEN]  alert_avgspeed_bg.flac ... ✓  (40.7 KB)
  [SKIP] alert_speedcam_bg: empty text
  [EXIST] alert_in_en.flac — already exists, skipping
  ...

────────────────────────────────────────
Done:    61
Skipped: 4
Errors:  0
```

---

## Adding a New Language

1. Add a new column to `tts_messages_all_languages.csv` with the language code as the header (e.g. `fr`).
2. Add a cell for that column in each `__` config row (copy the default from a neighbouring column). If you leave them blank, the script falls back to its built-in defaults.
3. Fill in translations for the message rows you need.
4. If the new language needs a more suitable voice or different delivery, edit its cell in `__voice_id` (and any of the other `__` rows).
5. Run the script — only the new (missing) files will be generated.

---

## How It Works

```
Read CSV → split "__" config rows from message rows
Resolve per-language settings (CSV @ cell → fallback → script default)

For each message row:
  For each language column with non-empty text:
    1. Look up that language's voice_id / model_id / speed / voice_settings
    2. POST to ElevenLabs API  →  receive MP3 bytes in memory
    3. Measure integrated loudness of the MP3 bytes (ffmpeg ebur128)
    4. Pipe MP3 bytes into ffmpeg via stdin  →  gain to the LUFS target
       (limiter at -1 dB), trim tail if --trim-end was passed  →  write
       {filename}_{lang}.flac
    5. Sleep 0.5 s to respect the 2 req/s rate limit
```

No temporary files are created. If a job fails, the error is logged and the script continues with the next cell. The final exit code is non-zero if any errors occurred.
