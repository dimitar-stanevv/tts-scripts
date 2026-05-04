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
cd new_unified_script
cp local_config.example.ini local_config.ini
# Edit local_config.ini and set api_key under [elevenlabs]
```

2. Format of `local_config.ini`:

```ini
[elevenlabs]
api_key = sk_your_key_here
```

**Precedence** (first match wins): `--api-key` → `ELEVENLABS_API_KEY` → `[elevenlabs] api_key` in the config file (default path: `new_unified_script/local_config.ini`, overridable with `--config`).

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

### Example

```csv
filename;description;en;de;bg
alert_avgspeed;Exceeding average speed alert;You are exceeding the average speed!;Sie überschreiten die Durchschnittsgeschwindigkeit!;Превишавате средната скорост!
alert_speedcam;Static speed camera danger;Speed camera ahead.;Achtung Blitzer.;Камера за скорост напред.
alert_in;;in;in;в
```

Empty cells are silently skipped — you can leave a cell blank if a translation is not yet available for a given language.

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

## Voice IDs

Voice IDs are configured **inside the script** (`generate_tts.py`), not in the CSV:

```python
DEFAULT_VOICE_ID = "WZlYpi1yf6zJhNWXih74"  # used for all languages unless overridden

VOICE_OVERRIDES: dict[str, str] = {
    "bg": "M1ydWt7KnBCiuv4CnEDC",
    # add more overrides as needed, keyed by language code
}
```

The script resolves the voice for each language as:

```
VOICE_OVERRIDES.get(lang, DEFAULT_VOICE_ID)
```

To set a different voice for a specific language, add an entry to `VOICE_OVERRIDES`. To change the global default, update `DEFAULT_VOICE_ID`.

---

## Voice Settings

| Parameter | Value |
|-----------|-------|
| Model | `eleven_multilingual_v2` |
| Speed | `1.05` (overridable via `--speed`) |
| Stability | 100 % |
| Similarity boost | 75 % |
| Style exaggeration | 0 % |
| Speaker boost | On |

---

## Usage

```bash
python generate_tts.py --csv tts_messages_new.csv [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--csv PATH` | *(required)* | Path to input CSV file |
| `--out DIR` | `./tts_output` | Directory where `.flac` files are saved (created if it does not exist) |
| `--config PATH` | `local_config.ini` next to `generate_tts.py` | INI file with `[elevenlabs] api_key = ...` |
| `--api-key KEY` | *(see API key section)* | Overrides env var and config file. |
| `--compression N` | `5` | FLAC compression level 0–12. Higher values produce smaller files at the cost of encoding time. |
| `--speed N` | `1.05` | TTS speed multiplier applied to all languages. |
| `--dry-run` | off | Preview all jobs without making API calls or running ffmpeg. |

---

## Examples

```bash
# Preview everything — no API calls, no ffmpeg
python generate_tts.py --csv tts_messages_new.csv --dry-run

# Generate all audio (reads key from local_config.ini if present)
python generate_tts.py --csv tts_messages_new.csv --out ./output

# Or pass the key explicitly (overrides config and env)
python generate_tts.py \
  --csv tts_messages_new.csv \
  --out ./output \
  --api-key sk-...

# Or use an environment variable (overrides config file only)
export ELEVENLABS_API_KEY="sk-..."
python generate_tts.py --csv tts_messages_new.csv --out ./output

# Maximum FLAC compression, slightly slower speech
python generate_tts.py \
  --csv tts_messages_new.csv \
  --out ./output \
  --compression 12 \
  --speed 1.0
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

1. Add a new column to `tts_messages_new.csv` with the language code as the header (e.g. `fr`).
2. Fill in translations for the rows you need.
3. If the new language requires a different ElevenLabs voice, add an entry to `VOICE_OVERRIDES` in `generate_tts.py`.
4. Run the script — only the new (missing) files will be generated.

---

## How It Works

```
For each row in CSV:
  For each language column with non-empty text:
    1. Resolve voice ID  →  VOICE_OVERRIDES.get(lang, DEFAULT_VOICE_ID)
    2. POST to ElevenLabs API  →  receive MP3 bytes in memory
    3. Pipe MP3 bytes into ffmpeg via stdin  →  write {filename}_{lang}.flac
    4. Sleep 0.5 s to respect the 2 req/s rate limit
```

No temporary files are created. If a job fails, the error is logged and the script continues with the next cell. The final exit code is non-zero if any errors occurred.
