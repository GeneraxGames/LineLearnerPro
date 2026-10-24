"""
    LineLearnerPro 1.2.0
    (C)opyright PeeJay 2026
    powered by Haribo Goldbears
"""

import asyncio
import ctypes
from ctypes import wintypes
import difflib
import io
import json
import os
import platform as platform_module
import re
import shutil
import subprocess
import tempfile
import urllib.request
import webbrowser
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import flet as ft
from PIL import Image
from piper import PiperVoice
import vosk
vosk.SetLogLevel(-1)  # otherwise Vosk prints a lot of debug noise to stderr by default
import sounddevice as sd

class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", wintypes.LPVOID),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", wintypes.LPVOID),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]

_OFN_EXPLORER = 0x00080000        # modern Explorer-style dialog (not the old-style one)
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_OVERWRITEPROMPT = 0x00000002
_OFN_NOCHANGEDIR = 0x00000008     # stop the dialog silently changing the process's CWD as a side effect

def _win32_file_filter_string(filetypes: list) -> str:
    """Converts tkinter-style filetypes [(label, pattern), ...] into the double-NUL-terminated filter string GetOpenFileNameW/GetSaveFileNameW expect"""
    parts = []
    for label, pattern in filetypes:
        parts.append(label)
        parts.append(pattern.replace(" ", ";"))
    return "\0".join(parts) + "\0\0"

def _win32_foreground_window() -> wintypes.HWND:
    """The window the user was just interacting with (they clicked a button in it right before this runs) - used as hwndOwner for the file dialog"""
    ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND
    return ctypes.windll.user32.GetForegroundWindow()

class Role:
    def __init__(self, character: str = "", gender: str = "",
                 photo: bytes | None = None, voice: str = ""):
        self.character = character  # the character's name AS WRITTEN IN THE SCRIPT (e.g. "EVE")
        self.gender = gender
        self.photo = photo          # resized PNG bytes (see resize_image_to_icon), or None for the default Janus mask
        self.voice = voice

# ---------------------------------------------------------------------------
# Script parsing
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^\[(.+)\]$", re.DOTALL)
_STAGE_DIRECTION_RE = re.compile(r"^\((.*)\)$", re.DOTALL)
_DIALOGUE_RE = re.compile(r"^([A-Z][A-Z0-9'\- ]*?)\s*:\s*(.*)$", re.DOTALL)
_INLINE_PARENS_RE = re.compile(r"\([^()]*\)")

def _strip_inline_directions(text: str) -> str:
    """Removes any (stage directions) embedded inside a dialogue line"""
    cleaned = _INLINE_PARENS_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()

STAGE_DIRECTION_COLOR = "#808080"  # RGB(128, 128, 128)

_INLINE_PARENS_SPLIT_RE = re.compile(r"(\([^()]*\))")

def build_transcript_line_spans(text: str, override_color: str | None = None) -> list:
    """Splits a line's raw text (exactly as it appeared in the script) into Flet TextSpans for rendering"""
    spans = []
    for part in _INLINE_PARENS_SPLIT_RE.split(text):
        if not part:
            continue
        if part.startswith("(") and part.endswith(")"):
            spans.append(ft.TextSpan(part, ft.TextStyle(italic=True, color=override_color or STAGE_DIRECTION_COLOR)))
        else:
            spans.append(ft.TextSpan(part, ft.TextStyle(color=override_color) if override_color else None))
    return spans

_ALL_CAPS_WORD_RE = re.compile(r"\b[A-Z][A-Z']*[A-Z]\b")

def _lowercase_shouted_words(text: str) -> str:
    """Remove all caps from spoken text to eliminate acronyms"""
    return _ALL_CAPS_WORD_RE.sub(lambda m: m.group(0).lower(), text)

_DOUBLE_QUOTE_CHARS_RE = re.compile(r'["\u201c\u201d]')

def _strip_double_quote_marks(text: str) -> str:
    """Removes literal double-quotation-mark characters (straight and curly) from text meant for TTS"""
    return _DOUBLE_QUOTE_CHARS_RE.sub("", text)

def _compute_base_spoken_text(text: str) -> str:
    """The TTS-ready version of a line's text BEFORE the pronunciation dictionary is applied"""
    return _strip_double_quote_marks(_lowercase_shouted_words(_strip_inline_directions(text)))

def parse_script_text(raw_text: str) -> dict:
    """Parses a script laid out per convention

    FORMAT ASSUMED:
      - Blocks (headers, stage directions, dialogue lines) are each separated by one or more blank lines.
      - A header is a WHOLE block on its own, wrapped in square brackets e.g. "[Act 1]", "[Scene 2]", "[Introduction]"
      - A block that is ENTIRELY wrapped in one pair of parentheses is a standalone stage direction
      - Everything else is a dialogue block: an UPPERCASE character name, a colon, then the line, which may itself contain inline parenthetical stage directions
      - No [Act] header is required at all - a one-act play can go straight to [Scene], and everything then sits under one implicit act.
      - An act can have no [Scene] headers of its own - its content is then treated as a single implicit scene
    """
    # Normalise line endings first - Windows-authored scripts (like sample) use \r\n
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    blocks = [b.strip() for b in re.split(r"\n\s*\n+", text)]
    blocks = [b for b in blocks if b]  # drop empty blocks (blank line runs, leading/trailing whitespace)

    acts = []
    current_act = None
    current_scene = None
    character_names = []  # preserves first-seen order, de-duplicated below via a set

    def ensure_act():
        nonlocal current_act, current_scene
        if current_act is None:
            current_act = {"label": None, "scenes": []}
            acts.append(current_act)
            current_scene = None

    def ensure_scene():
        nonlocal current_scene
        ensure_act()
        if current_scene is None:
            current_scene = {"label": None, "blocks": []}
            current_act["scenes"].append(current_scene)

    seen_characters = set()

    for block in blocks:
        header_match = _HEADER_RE.match(block)

        if header_match:
            label = header_match.group(1).strip()
            if re.match(r"(?i)^act\b", label):
                current_act = {"label": label, "scenes": []}
                acts.append(current_act)
                current_scene = None
            else:
                # Covers both explicit "Scene N" headers and any other named heading (e.g. "Introduction") - both open a new scene under whatever the current act is.
                ensure_act()
                current_scene = {"label": label, "blocks": []}
                current_act["scenes"].append(current_scene)
            continue

        direction_match = _STAGE_DIRECTION_RE.match(block)
        if direction_match:
            ensure_scene()
            current_scene["blocks"].append({
                "type": "direction",
                "text": direction_match.group(1).strip(),
            })
            continue

        dialogue_match = _DIALOGUE_RE.match(block)
        if dialogue_match:
            character = dialogue_match.group(1).strip()
            spoken_line = dialogue_match.group(2).strip()
            ensure_scene()
            current_scene["blocks"].append({
                "type": "line",
                "character": character,
                "text": spoken_line,                                # full line, directions left in
                "spoken_text": _compute_base_spoken_text(spoken_line),  # what TTS should actually read
            })
            if character not in seen_characters:
                seen_characters.add(character)
                character_names.append(character)
            continue

        # Anything that matches none of the above (malformed input, a stray line without a character prefix, etc.) is kept rather
        # than silently dropped, so nothing from the source script goes missing without a trace - it just can't be usefully classified.
        ensure_scene()
        current_scene["blocks"].append({"type": "unrecognised", "text": block})

    return {"acts": acts, "characters": character_names}

def act_display_label(act: dict, index: int) -> str:
    """A human-friendly label for an act"""
    return act["label"] or f"Act {index + 1}"

def scene_display_label(scene: dict, index: int) -> str:
    """Same idea as act_display_label, for scenes"""
    return scene["label"] or f"Scene {index + 1}"

def get_blocks_for_scope(parsed_script: dict, act_index, scene_index) -> list:
    """Flattens the chosen scope into one ordered list of blocks, ready to walk through sequentially - the same act_index/scene_index shape
    build_read_aloud_scope_options attaches to each menu option:
      - act_index=None -> every act, i.e. the whole script
      - act_index set, scene_index=None -> every scene in that one act
      - both set -> just that one scene
    """
    acts = parsed_script["acts"]
    selected_acts = acts if act_index is None else [acts[act_index]]

    blocks = []
    for act in selected_acts:
        if act_index is not None and scene_index is not None:
            scenes = [act["scenes"][scene_index]]
        else:
            scenes = act["scenes"]
        for scene in scenes:
            blocks.extend(scene["blocks"])
    return blocks

def get_distinct_characters_for_scope(blocks: list) -> list:
    """The distinct set of characters who actually speak within a given list of blocks, in first-appearance order"""
    seen = []
    for block in blocks:
        if block["type"] != "line" or not block["spoken_text"]:
            continue
        character = block["character"]
        if character not in seen:
            seen.append(character)
    return seen

def get_blocks_with_scope_context(parsed_script: dict, act_index, scene_index) -> list:
    """Same scope selection as get_blocks_for_scope, but each block is paired with the act/scene label it belongs to"""
    acts = parsed_script["acts"]
    single_act_play = len(acts) == 1
    selected_acts = list(enumerate(acts)) if act_index is None else [(act_index, acts[act_index])]

    entries = []
    for a_index, act in selected_acts:
        act_label = None if single_act_play else act_display_label(act, a_index)
        if act_index is not None and scene_index is not None:
            selected_scenes = [(scene_index, act["scenes"][scene_index])]
        else:
            selected_scenes = list(enumerate(act["scenes"]))
        for s_index, scene in selected_scenes:
            scene_label = scene_display_label(scene, s_index)
            for block in scene["blocks"]:
                entries.append((block, act_label, scene_label))
    return entries

# Smart Cue: how far ahead of the user's own next line it aims to land, and the assumed reading pace used to estimate that from word counts
SMART_CUE_TARGET_SECONDS = 30
SMART_CUE_ESTIMATED_WORDS_PER_MINUTE = 120

# How long each skipped block's brief appearance in the transcript lingers before the next one during Smart Cue's fast-forward pass
SMART_CUE_SKIP_STEP_SECONDS = 0.12

def compute_smart_cue_target_index(
    entries: list,
    current_index: int,
    my_selected_roles: set,
    target_seconds: float = SMART_CUE_TARGET_SECONDS,
    words_per_minute: float = SMART_CUE_ESTIMATED_WORDS_PER_MINUTE,
):
    """Where Smart Cue should jump to: about target_seconds of estimated reading time before the user's next own line"""
    next_own_line_index = None
    for i in range(current_index, len(entries)):
        block = entries[i][0]
        if block["type"] == "line" and block["character"] in my_selected_roles:
            next_own_line_index = i
            break
    if next_own_line_index is None:
        return None

    def block_seconds(block: dict) -> float:
        if block["type"] != "line" or not block.get("spoken_text"):
            return 0.0  # only spoken lines take estimated time - directions are shown instantly
        word_count = len(block["spoken_text"].split())
        return word_count / words_per_minute * 60

    total_seconds = sum(block_seconds(entries[i][0]) for i in range(current_index, next_own_line_index))
    if total_seconds <= target_seconds:
        return None  # already closer than the target - nothing worth skipping

    cumulative = 0.0
    target_index = current_index
    for i in range(next_own_line_index - 1, current_index - 1, -1):
        cumulative += block_seconds(entries[i][0])
        target_index = i
        if cumulative >= target_seconds:
            break

    if target_index <= current_index:
        # The only way to reach target_seconds of runway required consuming the CURRENT line's own entire duration
        return None

    return target_index

def build_read_aloud_scope_options(parsed_script: dict) -> list:
    """Builds the list of scope choices ReadScriptAloud's menu should offer"""
    if not parsed_script:
        return []
    acts = parsed_script["acts"]
    single_act_play = len(acts) == 1
    options = []

    for act_index, act in enumerate(acts):
        if len(act["scenes"]) > 1:
            act_label = act_display_label(act, act_index)
            for scene_index, scene in enumerate(act["scenes"]):
                scene_label = scene_display_label(scene, scene_index)
                label = f"Read {scene_label}" if single_act_play else f"Read {act_label} {scene_label}"
                options.append({
                    "label": label,
                    "act_index": act_index,
                    "scene_index": scene_index,
                })

    if len(acts) > 1:
        for act_index, act in enumerate(acts):
            act_label = act_display_label(act, act_index)
            options.append({
                "label": f"Read Complete {act_label}",
                "act_index": act_index,
                "scene_index": None,
            })

    options.append({
        "label": "Read Complete Script",
        "act_index": None,
        "scene_index": None,
    })

    return options

# ---------------------------------------------------------------------------
# Cast photo handling
# ---------------------------------------------------------------------------

ICON_MAX_DIMENSION = 256 # matches the Janus mask images

def resize_image_to_icon(raw_bytes: bytes) -> bytes:
    """Takes whatever image the user picked and returns PNG bytes: longest side capped at ICON_MAX_DIMENSION"""
    with Image.open(io.BytesIO(raw_bytes)) as img:
        img = img.convert("RGBA")
        img.thumbnail((ICON_MAX_DIMENSION, ICON_MAX_DIMENSION), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

# ---------------------------------------------------------------------------
# Voice handling
# ---------------------------------------------------------------------------

AUDITION_TEXT = "This is my voice to help you with your rehearsals."

# A small, hand-picked subset of Piper's 100+ voice catalogue

def _voices_json_path() -> Path:
    """Lives inside assets/ specifically"""
    return Path(__file__).resolve().parent / "assets" / "voices.json"

def _load_curated_voices() -> list:
    """Loads the curated voice list from assets/voices.json"""
    path = _voices_json_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            voices = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Couldn't find the curated voices file at {path} - was it deleted or renamed?")
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"assets/voices.json isn't valid JSON: {ex}")
    if not isinstance(voices, list) or not voices:
        raise RuntimeError("assets/voices.json must contain a non-empty JSON array of voice entries")
    for entry in voices:
        missing = [k for k in ("id", "label", "gender") if k not in entry]
        if missing:
            raise RuntimeError(f"A voice entry in assets/voices.json is missing required field(s) {missing}: {entry}")
    return voices

CURATED_VOICES = _load_curated_voices()

# Used by ReadScriptAloud for any role that doesn't currently have a usable voice of its own
FALLBACK_VOICE_ID = "en_GB-alan-medium"

DONATION_URL = "https://paypal.me/linelearnerpro/5GBP"

def _app_data_dir() -> Path:
    """Where LineLearnerPro keeps everything it downloads/generates locally - voice models, cached audition clips, preferences, and the pronunciation dictionary. Ensures the folder exists on every call, since callers write directly under it (not just into an already-mkdir'd subdirectory)."""
    d = Path.home() / ".linelearnerpro"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _voices_cache_dir() -> Path:
    d = _app_data_dir() / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _audition_cache_dir() -> Path:
    d = _app_data_dir() / "auditions"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _hearing_quality_json_path() -> Path:
    """Lives inside assets/ specifically, same reasoning as _voices_json_path - assets/ is what gets bundled"""
    return Path(__file__).resolve().parent / "assets" / "hearing_quality.json"

def _load_hearing_quality_tiers() -> list:
    """Loads the "Hearing quality" tier catalog (Low/Medium/High, each mapping to a real Vosk model)"""
    path = _hearing_quality_json_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            tiers = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Couldn't find the hearing quality catalog at {path} - was it deleted or renamed?")
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"assets/hearing_quality.json isn't valid JSON: {ex}")
    if not isinstance(tiers, list) or not tiers:
        raise RuntimeError("assets/hearing_quality.json must contain a non-empty JSON array of tier entries")
    for entry in tiers:
        missing = [k for k in ("tier", "label", "model_name", "size_mb") if k not in entry]
        if missing:
            raise RuntimeError(f"A tier entry in assets/hearing_quality.json is missing required field(s) {missing}: {entry}")
    return tiers

HEARING_QUALITY_TIERS = _load_hearing_quality_tiers()

def _hearing_quality_tier_by_name(tier: str) -> dict | None:
    """Looks up one tier's catalog entry by its short name - returns None if it doesn't match anything"""
    return next((t for t in HEARING_QUALITY_TIERS if t["tier"] == tier), None)

def _vosk_model_dir(model_name: str) -> Path:
    """Keyed by model_name itself, not a fixed generic folder name"""
    return _app_data_dir() / model_name

def is_vosk_model_downloaded(model_name: str) -> bool:
    d = _vosk_model_dir(model_name)
    return d.is_dir() and any(d.iterdir())

def download_vosk_model_sync(model_name: str) -> None:
    """BLOCKING - downloads and extracts the given Vosk speech-recognition model, if not already present"""
    if is_vosk_model_downloaded(model_name):
        return

    url = f"{vosk.MODEL_PRE_URL}{model_name}.zip"
    with urllib.request.urlopen(url) as response:
        zip_bytes = response.read()

    _app_data_dir().mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(_app_data_dir())

def delete_vosk_model_sync(model_name: str) -> None:
    """BLOCKING - removes a downloaded model's directory entirely"""
    d = _vosk_model_dir(model_name)
    if d.exists():
        shutil.rmtree(d)

def _max_plausible_word_duration() -> float:
    """A genuine single spoken word, even a long one, realistically never takes very long - but this has to stay safely BELOW
    SILENCE_THRESHOLD_SECONDS specifically, not just some fixed number
    The 0.9s floor matters in the OTHER direction: SILENCE_THRESHOLD_SECONDS is tuned for UX feel (how long auto-advance waits)"""
    return max(0.9, SILENCE_THRESHOLD_SECONDS * (2 / 3))

def _filter_spurious_long_words(raw_result: dict) -> str:
    """Rebuilds recognized text from Result()/FinalResult()'s raw JSON"""
    words = raw_result.get("result")
    if not words:
        return raw_result.get("text", "")
    max_duration = _max_plausible_word_duration()
    kept = [
        w["word"] for w in words
        if (w.get("end", 0) - w.get("start", 0)) <= max_duration
    ]
    return " ".join(kept)

_vosk_model_cache = None
_vosk_model_cache_name = None

def _get_vosk_model(model_name: str):
    """Returns an already-loaded Vosk Model, loading and caching it the first time it's needed for THIS model_name"""
    global _vosk_model_cache, _vosk_model_cache_name
    if _vosk_model_cache is None or _vosk_model_cache_name != model_name:
        _vosk_model_cache = vosk.Model(str(_vosk_model_dir(model_name)))
        _vosk_model_cache_name = model_name
    return _vosk_model_cache

def _pronunciation_dictionary_path() -> Path:
    """Lives in the per-user app data folder alongside preferences.json"""
    return _app_data_dir() / "pronunciation.json"

def load_pronunciation_dictionary_sync() -> list:
    """Returns a list of {"word": ..., "phonetic": ...} dicts"""
    path = _pronunciation_dictionary_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []

def save_pronunciation_dictionary_sync(entries: list) -> None:
    """Always writes alphabetically sorted by word (case-insensitive)"""
    sorted_entries = sorted(entries, key=lambda e: e["word"].lower())
    _pronunciation_dictionary_path().write_text(
        json.dumps(sorted_entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )

def apply_pronunciation_dictionary(text: str, entries: list) -> str:
    """Replaces any word appearing in the pronunciation dictionary with its phonetic respelling, case-insensitively"""
    result = text
    for entry in entries:
        word = entry["word"]
        phonetic = entry["phonetic"]
        if not word:
            continue
        pattern = re.compile(r"\b" + re.escape(word) + r"(s|ing)?\b", re.IGNORECASE)

        def _replace(m, phonetic=phonetic):
            suffix = m.group(1) or ""
            return phonetic + suffix

        result = pattern.sub(_replace, result)
    return result

def apply_pronunciation_dictionary_to_script(parsed_script: dict, entries: list) -> None:
    """Walks every dialogue line across the whole parsed script and rewrites its spoken_text in place"""
    if not entries:
        return
    for act in parsed_script["acts"]:
        for scene in act["scenes"]:
            for block in scene["blocks"]:
                if block["type"] == "line" and block["spoken_text"]:
                    block["spoken_text"] = apply_pronunciation_dictionary(block["spoken_text"], entries)

def reapply_pronunciation_dictionary_to_parsed_script(parsed_script: dict, entries: list) -> None:
    """For an ALREADY-parsed script (e.g. a loaded .llp, where script.json already holds both "text" and a previously-derived "spoken_text" for
    every line) - regenerates spoken_text fresh from the untouched "text" field for every line first, THEN applies the CURRENT dictionary."""
    for act in parsed_script["acts"]:
        for scene in act["scenes"]:
            for block in scene["blocks"]:
                if block["type"] == "line":
                    block["spoken_text"] = _compute_base_spoken_text(block["text"])
    apply_pronunciation_dictionary_to_script(parsed_script, entries)

def _voice_model_paths(voice_id: str) -> tuple:
    """Where a given voice's .onnx model + .onnx.json config live once downloaded, regardless of whether they're actually there yet."""
    cache_dir = _voices_cache_dir()
    return cache_dir / f"{voice_id}.onnx", cache_dir / f"{voice_id}.onnx.json"

def is_voice_downloaded(voice_id: str) -> bool:
    model_path, config_path = _voice_model_paths(voice_id)
    return model_path.exists() and config_path.exists()

_MIN_VOICE_MODEL_BYTES = 1_000_000  # real Piper models are tens of MB - anything smaller is a stub or an error page, not a model

def _download_to_file_verified(url: str, dest: Path, attempts: int = 3) -> None:
    """BLOCKING - downloads `url` to `dest`, guaranteeing a short/failed download is NEVER left at the final filename.
    Piper's own downloader writes straight to the final path, and urllib returns a SHORT body with NO exception if the connection drops
    mid-file - so a half-downloaded .onnx looked "downloaded" forever, then failed later inside ONNX Runtime as INVALID_PROTOBUF.
    Here the data goes to a temp file first, the byte count is checked against Content-Length, and only then is it moved into place."""
    import time
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(1, attempts + 1):
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                with urllib.request.urlopen(url, timeout=60) as response:
                    expected = response.headers.get("Content-Length")
                    shutil.copyfileobj(response, tmp_file)
            received = os.path.getsize(tmp_name)
            if received == 0:
                raise IOError("empty download")
            if expected is not None and received != int(expected):
                raise IOError(f"incomplete download - got {received:,} of {int(expected):,} bytes")
            os.replace(tmp_name, dest)
            return
        except Exception as ex:
            last_error = ex
        finally:
            if os.path.exists(tmp_name):  # still here = never made it into place, so don't leave it behind
                os.unlink(tmp_name)
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise IOError(f"Couldn't download {dest.name} after {attempts} attempts ({last_error})")

def download_voice_sync(voice_id: str) -> None:
    """BLOCKING - downloads a voice's model + config from Piper's own official Hugging Face repository into the local cache, if not already present.
    Uses Piper's own URL layout and voice-id parsing, but NOT Piper's download function (see _download_to_file_verified for why)."""
    from piper.download_voices import URL_FORMAT, VOICE_PATTERN
    match = VOICE_PATTERN.match(voice_id.strip())
    if not match:
        raise ValueError(f"Voice '{voice_id}' did not match pattern: <language>-<name>-<quality> like 'en_US-lessac-medium'")
    lang_family = match.group("lang_family")
    format_args = {
        "lang_family": lang_family,
        "lang_code": lang_family + "_" + match.group("lang_region"),
        "voice_name": match.group("voice_name"),
        "voice_quality": match.group("voice_quality"),
    }
    model_path, config_path = _voice_model_paths(voice_id)
    if not model_path.exists() or model_path.stat().st_size < _MIN_VOICE_MODEL_BYTES:
        _download_to_file_verified(URL_FORMAT.format(extension=".onnx", **format_args), model_path)
    if not config_path.exists() or config_path.stat().st_size == 0:
        _download_to_file_verified(URL_FORMAT.format(extension=".onnx.json", **format_args), config_path)

def delete_voice_sync(voice_id: str) -> None:
    """BLOCKING - removes a downloaded voice's model+config files, and its per-user CACHED audition clip (~/.linelearnerpro/auditions/)"""
    model_path, config_path = _voice_model_paths(voice_id)
    model_path.unlink(missing_ok=True)
    config_path.unlink(missing_ok=True)
    _audition_clip_path(voice_id).unlink(missing_ok=True)
    _loaded_voice_cache.pop(voice_id, None)

_loaded_voice_cache = {}

def _get_loaded_voice(voice_id: str) -> PiperVoice:
    """Returns an already-loaded PiperVoice for this voice_id, loading and caching it the first time it's needed."""
    if voice_id in _loaded_voice_cache:
        return _loaded_voice_cache[voice_id]

    model_path, config_path = _voice_model_paths(voice_id)
    try:
        voice = PiperVoice.load(model_path, config_path)
    except Exception:
        model_path.unlink(missing_ok=True)
        config_path.unlink(missing_ok=True)
        download_voice_sync(voice_id)
        voice = PiperVoice.load(model_path, config_path)

    _loaded_voice_cache[voice_id] = voice
    return _loaded_voice_cache[voice_id]

_SENTENCE_SPLIT_RE = re.compile(
    r"(?:\s+-\s+|(?<=[.!?])\s+|(?<=[.!?][\"'\u2018\u2019\u201c\u201d)\]])\s+)"
)
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;:])\s+")
LONG_SENTENCE_WORD_THRESHOLD = 20

def _split_into_sentences(text: str) -> list:
    """A deliberately simple sentence splitter - good enough for a play script, not aiming for perfect handling of abbreviations/decimals"""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return sentences if sentences else [text]

def _split_long_sentence_into_clauses(sentence: str, max_words: int = LONG_SENTENCE_WORD_THRESHOLD) -> list:
    """Further splits a sentence at clause boundaries (commas, semicolons, colons) if it's longer than max_words - short sentences are left whole"""
    if len(sentence.split()) <= max_words:
        return [sentence]
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(sentence) if c.strip()]
    return clauses if len(clauses) > 1 else [sentence]

def _split_text_for_synthesis(text: str) -> list:
    """The units synthesize_wav_bytes_sync hands to Piper, one call each"""
    return [_split_long_sentence_into_clauses(sentence) for sentence in _split_into_sentences(text)]

SENTENCE_PAUSE_SECONDS = 0.35  # a short "beat" between sentences - not a dramatic pause, just enough that they don't run directly into each other

CURSOR_HIDE_DELAY_SECONDS = 3  # how long the mouse cursor stays visible on the reading screen after the last movement, before hiding again

SILENCE_THRESHOLD_SECONDS = 1.2  # how long the user's speech needs to have gone quiet before Rehearse My Lines auto-advances

LINE_MATCH_THRESHOLD = 0.75  # Rehearse My Lines' scoring cutoff - a line whose recognized speech scores at or above this is assumed correct

_PUNCTUATION_FOR_COMPARISON_RE = re.compile(r"[^\w\s']")  # strips punctuation but keeps apostrophes, not separator punctuation

def _normalize_words_for_comparison(text: str) -> list:
    """Lowercase, strip punctuation (apostrophes kept), split into words"""
    text = _PUNCTUATION_FOR_COMPARISON_RE.sub("", text.lower())
    return text.split()

def _score_heard_text(expected_spoken_text: str, heard_text: str) -> float:
    """0.0-1.0 similarity between what was actually recognized and the script's expected line - word by word, not character by character"""
    expected_words = _normalize_words_for_comparison(expected_spoken_text)
    heard_words = _normalize_words_for_comparison(heard_text)
    if not expected_words:
        return 1.0  # nothing was ever expected to be said here - not a real case in practice, just a safe default
    return difflib.SequenceMatcher(None, expected_words, heard_words).ratio()

def synthesize_wav_bytes_sync(voice_id: str, text: str) -> bytes:
    """BLOCKING - synthesizes ONE unit of text (a whole sentence, or one clause-piece of an unusually long sentence using an already-downloaded voice"""
    voice = _get_loaded_voice(voice_id)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        header_set = False
        expected_params = None
        for chunk in voice.synthesize(text):
            params = (chunk.sample_rate, chunk.sample_width, chunk.sample_channels)
            if not header_set:
                wav_file.setnchannels(chunk.sample_channels)
                wav_file.setsampwidth(chunk.sample_width)
                wav_file.setframerate(chunk.sample_rate)
                expected_params = params
                header_set = True
            elif params != expected_params:
                continue  # anomalous chunk - skip rather than write mismatched audio into this header
            wav_file.writeframes(chunk.audio_int16_bytes)

        if not header_set:
            # Piper produced NO audio chunks at all for this unit
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)

    return buf.getvalue()

def _bundled_audition_clip_path(voice_id: str) -> Path:
    """Where a PRE-GENERATED, bundled audition clip would live if one ships with the app"""
    return Path(__file__).resolve().parent / "assets" / "auditions" / f"{voice_id}.wav"

def _audition_clip_path(voice_id: str) -> Path:
    return _audition_cache_dir() / f"{voice_id}.wav"

def ensure_audition_clip_sync(voice_id: str) -> Path:
    """BLOCKING - the whole "get me a ready-to-play audition clip for this voice" pipeline"""
    bundled_path = _bundled_audition_clip_path(voice_id)
    if bundled_path.exists():
        return bundled_path

    if not is_voice_downloaded(voice_id):
        download_voice_sync(voice_id)

    clip_path = _audition_clip_path(voice_id)
    if not clip_path.exists():
        wav_bytes = synthesize_wav_bytes_sync(voice_id, AUDITION_TEXT)
        clip_path.write_bytes(wav_bytes)

    return clip_path

def download_voice_and_ensure_audition_sync(voice_id: str) -> None:
    """BLOCKING - the combined "make this voice genuinely usable" pipeline: downloads the model UNCONDITIONALLY"""
    download_voice_sync(voice_id)
    ensure_audition_clip_sync(voice_id)

def play_wav_file_sync(path: Path) -> None:
    """BLOCKING - plays a WAV file using whichever mechanism is safest for the current OS, DELIBERATELY avoiding flet_audio's Audio control on desktop.
      - Windows: winsound (standard library - no extra dependency at all)
      - macOS: the built-in `afplay` command
      - Linux: `aplay` (ALSA, the common default), falling back to `paplay` (PulseAudio) if aplay isn't installed
    winsound is imported lazily, INSIDE the Windows branch only - it's a Windows-only stdlib module, importing it at the top of this file would crash the app immediately on macOS/Linux.
    """
    system = platform_module.system()
    if system == "Windows":
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    elif system == "Darwin":
        subprocess.run(["afplay", str(path)], check=True)
    else:
        try:
            subprocess.run(["aplay", str(path)], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            subprocess.run(["paplay", str(path)], check=True)

def play_wav_bytes_sync(wav_bytes: bytes) -> None:
    """BLOCKING - plays WAV audio straight from memory, with NO file written at any point on Windows or Linux.
      - Windows: winsound.PlaySound(bytes, SND_MEMORY) - plays straight from the bytes, confirmed against the real stdlib docs.
      - Linux: piped directly into aplay/paplay's stdin - no temp file.
      - macOS: afplay has no documented way to accept audio via stdin or memory, only a filename - so this is the one platform where a
        temp file is unavoidable. It's created immediately before playback and deleted immediately after (try/finally), so it's
        transient, not accumulating - it never survives past the single line it was created for."""
    system = platform_module.system()
    if system == "Windows":
        import winsound
        winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
    elif system == "Darwin":
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            temp_path = f.name
        try:
            subprocess.run(["afplay", temp_path], check=True)
        finally:
            os.unlink(temp_path)
    else:
        try:
            subprocess.run(["aplay", "-"], input=wav_bytes, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            subprocess.run(["paplay"], input=wav_bytes, check=True)

def voice_label_for(voice_id: str) -> str:
    """Display label for an already-assigned voice ID - falls back to the raw ID itself if it's somehow not in the curated list"""
    for voice in CURATED_VOICES:
        if voice["id"] == voice_id:
            return voice["label"]
    return voice_id

# ---------------------------------------------------------------------------
# .llp bundle building
# ---------------------------------------------------------------------------

LLP_FORMAT_VERSION = 1

def safe_filename(name: str) -> str:
    """Turns a character name into something safe to use as a zip entry filename - strips anything that isn't alphanumeric/underscore/hyphen"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return cleaned or "role"

def build_llp_bundle(title: str, parsed_script: dict, roles: list, resolve_photo_bytes) -> bytes:
    """Builds the actual .llp file contents (a zip archive) as bytes"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({
            "format_version": LLP_FORMAT_VERSION,
            "title": title,
            "created": datetime.now(timezone.utc).isoformat(),
        }, indent=2))

        z.writestr("script.json", json.dumps(parsed_script, indent=2))

        roles_data = []
        used_names = set()
        for role in roles:
            base_name = safe_filename(role.character)
            # Guard against two different character names sanitising down to the same safe filename (e.g. "Mc'Duff" and "McDuff")
            safe_name = base_name
            suffix = 2
            while safe_name in used_names:
                safe_name = f"{base_name}_{suffix}"
                suffix += 1
            used_names.add(safe_name)

            photo_bytes = resolve_photo_bytes(role)
            if photo_bytes:
                photo_path = f"photos/{safe_name}.png"
                z.writestr(photo_path, photo_bytes)
            else:
                photo_path = None

            roles_data.append({
                "character": role.character,
                "gender": role.gender,
                "voice": role.voice,
                "photo": photo_path,
            })

        z.writestr("roles.json", json.dumps({"roles": roles_data}, indent=2))

    return buf.getvalue()

def parse_llp_bundle(bundle_bytes: bytes) -> dict:
    """The reverse of build_llp_bundle. Unzips an .llp file's bytes and returns {"manifest": ..., "script": ..., "roles": [...]}
    Raises ValueError, with a message written to be shown to the user as-is, for anything that doesn't look like a valid, understood .llp"""
    try:
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as z:
            names = set(z.namelist())
            required = {"manifest.json", "script.json", "roles.json"}
            missing = required - names
            if missing:
                raise ValueError(
                    f"Not a valid LineLearnerPro file - missing {', '.join(sorted(missing))}."
                )

            manifest = json.loads(z.read("manifest.json"))
            format_version = manifest.get("format_version")
            if not isinstance(format_version, int) or format_version > LLP_FORMAT_VERSION:
                raise ValueError(
                    f"This file was saved by a newer version of LineLearnerPro "
                    f"(format {format_version}) - please update the app."
                )

            script_data = json.loads(z.read("script.json"))
            roles_raw = json.loads(z.read("roles.json")).get("roles", [])

            roles = []
            for r in roles_raw:
                photo_path = r.get("photo")
                photo_bytes = z.read(photo_path) if photo_path and photo_path in names else None
                roles.append(Role(
                    character=r.get("character", ""),
                    gender=r.get("gender", ""),
                    photo=photo_bytes,
                    voice=r.get("voice", ""),
                ))

            return {"manifest": manifest, "script": script_data, "roles": roles}
    except zipfile.BadZipFile:
        raise ValueError("This doesn't look like a valid LineLearnerPro (.llp) file.")
    except (KeyError, json.JSONDecodeError) as ex:
        raise ValueError(f"This LineLearnerPro file appears to be corrupted: {ex}")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class LineLearnerProApp:

    # Minimum touch target height in logical pixels
    BUTTON_HEIGHT = 52
    MAX_CONTENT_WIDTH = 480  # keeps menus from stretching edge-to-edge on wide desktop windows

    # Maps menu_mode -> the name of the method that navigates "back" from it
    MENU_MODE_BACK_HANDLER_NAMES = {
        1: "return_to_main_menu",         # Script Tools
        2: "return_to_script_tools",      # Assign Photos
        3: "return_to_script_tools",      # Assign Voices
        4: "return_to_assign_voices",     # Select Voice
        5: "return_to_main_menu",         # Scope Selection
        6: "return_to_main_menu",         # Select My Roles
        7: "return_to_settings",          # Pronunciation Dictionary List
        8: "_cancel_pronunciation_edit",  # Pronunciation Edit
        9: "return_to_main_menu",         # Settings
        10: "return_to_settings",         # Hearing Quality
        11: "return_to_settings",         # Voice Manager
        12: "return_to_settings",         # Help hub
        13: "return_to_help",             # Help - Setting up a script
        14: "return_to_help",             # Help - Rehearsing
        15: "return_to_help",             # Help - Settings
        16: "return_to_help",             # Help - What is LineLearnerPro?
    }

    def __init__(self, page: ft.Page):
        self.page = page
        self.dark_mode = True

        # Shared across every file dialog, regardless of type
        self._last_file_dialog_dir: str | None = None

        self.menu_mode = 0

        self.roles: list[Role] = []

        self.loaded_script_filename: str | None = None
        self.loaded_script_path: Path | None = None  # desktop only - see load_txt_script
        self.raw_script_text: str | None = None
        self.parsed_script: dict | None = None  # set by AnalyseScript

        self._voice_picker_role: "Role | None" = None

        self._voices_currently_loading: set = set()

        self._downloading_voices: list | None = None
        self._downloading_voices_current: str | None = None

        # "Hearing quality" (the user-facing name for which Vosk model is in use
        self.hearing_quality_tier: str | None = None
        self._hearing_quality_downloading: set = set()

        self._preparing_voices: list | None = None
        self._preparing_voices_current: str | None = None

        # Whether the "Program Initialising" screen should show BEFORE the main menu ever appears
        self._app_initialising: bool = False

        self.my_selected_roles: set = set()

        # The pronunciation dictionary - loaded once here, kept in memory, and re-saved to disk whenever an entry is added, edited, or deleted
        self.pronunciation_entries: list = load_pronunciation_dictionary_sync()

        # Which entry (if any) the pronunciation-dictionary edit screen is currently working on - None means "adding a new word".
        self._pronunciation_editing_word: str | None = None

        self._read_aloud_scope: dict | None = None

        self._read_aloud_active: bool = False

        self._read_aloud_stop_requested: bool = False

        self._read_aloud_current_act_label: str = ""
        self._read_aloud_current_scene_label: str = ""

        self._read_aloud_transcript: list = []

        # The current scope's flattened (block, act_label, scene_label) entries, and which index
        # within them reading has reached - both needed outside _read_lines_for_scope's own loop so
        # Smart Cue can inspect/redirect them. See compute_smart_cue_target_index.
        self._read_aloud_entries: list = []
        self._read_aloud_block_index: int = 0

        # Set by Smart Cue to interrupt whatever's currently playing and jump reading to this index
        # instead of advancing normally by one - cleared once that jump has happened.
        self._read_aloud_skip_to_index: int | None = None

        # Reference to the currently-mounted Smart Cue lozenge, so its enabled/disabled look can be
        # refreshed in place as reading progresses, same reasoning as _read_aloud_reading_text below.
        self._smart_cue_button = None

        # References to the CURRENTLY-MOUNTED "Reading: ..." Text and transcript ListView controls
        self._read_aloud_reading_text = None
        self._read_aloud_listview = None

        # The GestureDetector wrapping the whole reading screen
        self._read_aloud_cursor_region = None
        self._cursor_hide_task = None

        # Which reading screen mode is currently active
        self._read_aloud_mode: str = "read_aloud"

        # True only while genuinely paused on the user's own line in read-along mode
        self._read_along_waiting_for_continue: bool = False
        self._read_along_continue_event = None  # a fresh asyncio.Event() per session

        # True once the chosen scope has finished naturally (every block processed) but BEFORE the reading screen actually exits
        self._read_aloud_waiting_at_end: bool = False

        # Rehearse My Lines' auto-advance: while genuinely "Listening..." on the user's own line, the microphone feeds a fresh Vosk recognizer
        self._speech_recognizer = None
        self._last_partial_transcript: str = ""
        self._heard_any_speech: bool = False
        self._recognized_segments: list = []  # accumulated Vosk-finalized segments for the current pause - see _handle_speech_chunk/_stop_listening
        self._silence_timeout_task = None

        self.has_unsaved_changes: bool = False

        self.is_desktop = (not page.web) and page.platform in (
            ft.PagePlatform.WINDOWS,
            ft.PagePlatform.MACOS,
            ft.PagePlatform.LINUX,
        )

        self.setup_page()
        self.load_media()
        self.setup_file_picker()
        self.setup_voice_player()
        self.setup_speech_recorder()
        self.load_user_preferences()  # determines self.hearing_quality_tier - must run before the check below
        self._app_initialising = self._needs_initial_setup()
        self.show_menu()  # shows either the "Program Initialising" screen or the main menu, depending on the flag just set
        if self._app_initialising:
            self.page.run_task(self._run_initial_setup_then_show_menu)

    # -----------------------------------------------------------------
    # Setup
    # -----------------------------------------------------------------

    def setup_page(self):
        self.page.title = "LineLearnerPro"
        self.page.theme_mode = ft.ThemeMode.DARK if self.dark_mode else ft.ThemeMode.LIGHT
        self.page.padding = 0
        self.page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
        self.page.scroll = ft.ScrollMode.ADAPTIVE  # small screens can scroll instead of clipping buttons - ADAPTIVE also keeps the scrollbar visible on desktop/web (not just while actively scrolling), per feedback that mousewheel-only scrolling isn't discoverable/reliable on all hardware

        # Full screen only on a DESKTOP build - mobile native builds (flet build apk/ipa) are already borderless by default
        self.page.window.full_screen = True

        self.page.bgcolor = ft.Colors.with_opacity(1, "#000040") if self.dark_mode else "#E0E0E0"

        self.page.on_keyboard_event = self._handle_keyboard_event

    def load_media(self):
        self.logo_image = "icon.png"                  # branding - same master image flet build also uses to generate the OS-level app icon, no separate copy needed
        self.default_photo_a = "janus_blue.png"      # frame 1: default cast photo
        self.default_photo_b = "janus_pink.png"       # frame 2: default cast photo

    def setup_file_picker(self):
        """Registers ONE shared FilePicker service - but ONLY on web/mobile.

        On desktop, load_txt_script uses tkinter instead"""
        if self.is_desktop:
            self.file_picker = None
            return

        self.file_picker = ft.FilePicker()
        self.page.overlay.append(self.file_picker)

        self.page.update()

    def setup_voice_player(self):
        """Registers an Audio service - but ONLY on web/mobile"""
        if self.is_desktop:
            self.voice_player = None
            return

        from flet_audio import Audio
        self.voice_player = Audio()
        self.page.overlay.append(self.voice_player)
        self.page.update()

    def setup_speech_recorder(self):
        """Microphone capture for Rehearse My Lines' auto-advance (see _start_listening) uses sounddevice"""
        self._speech_stream = None

    def _determine_hearing_quality_tier(self, saved_tier: str | None) -> str:
        """Figures out which Hearing Quality tier should be active at startup"""
        if saved_tier and _hearing_quality_tier_by_name(saved_tier):
            return saved_tier

        for tier_entry in HEARING_QUALITY_TIERS:
            if is_vosk_model_downloaded(tier_entry["model_name"]):
                return tier_entry["tier"]

        return "low"

    def _needs_initial_setup(self) -> bool:
        """True if EITHER the fallback voice or the currently-selected Hearing Quality tier's model is missing"""
        if not is_voice_downloaded(FALLBACK_VOICE_ID):
            return True
        tier_entry = _hearing_quality_tier_by_name(self.hearing_quality_tier)
        return tier_entry is None or not is_vosk_model_downloaded(tier_entry["model_name"])

    async def _run_initial_setup_then_show_menu(self):
        """Runs while the "Program Initialising" screen is showing (see _needs_initial_setup/build_menu)"""
        tasks = []
        if not is_voice_downloaded(FALLBACK_VOICE_ID):
            tasks.append(self._download_fallback_voice_silently())

        tier_entry = _hearing_quality_tier_by_name(self.hearing_quality_tier)
        if tier_entry is not None and not is_vosk_model_downloaded(tier_entry["model_name"]):
            tasks.append(self._download_vosk_model_silently(tier_entry["model_name"]))

        if tasks:
            await asyncio.gather(*tasks)

        self._app_initialising = False
        self.build_menu()

    async def _download_vosk_model_silently(self, model_name: str):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, download_vosk_model_sync, model_name)
        except Exception:
            pass

    async def _download_fallback_voice_silently(self):
        """Uses download_voice_and_ensure_audition_sync, not the bare ensure_audition_clip_sync"""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, download_voice_and_ensure_audition_sync, FALLBACK_VOICE_ID)
        except Exception:
            pass

    def _preferences_file_path(self) -> Path:
        """Where the preferences file lives - the per-user app data folder, NOT next to main.py"""
        return _app_data_dir() / "preferences.json"

    def load_user_preferences(self):
        """If a preferences file is present, restores display mode immediately"""
        prefs_path = self._preferences_file_path()
        data = {}
        if prefs_path.exists():
            try:
                data = json.loads(prefs_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}  # corrupt/unreadable preferences - just start fresh, not worth crashing startup over

        dark_mode = data.get("dark_mode")
        if isinstance(dark_mode, bool):
            self.dark_mode = dark_mode
            self.page.theme_mode = ft.ThemeMode.DARK if self.dark_mode else ft.ThemeMode.LIGHT
            self.page.bgcolor = "#000040" if self.dark_mode else "#E0E0E0"

        my_selected_roles = data.get("my_selected_roles")
        if isinstance(my_selected_roles, list):
            self.my_selected_roles = set(my_selected_roles)

        last_llp = data.get("last_loaded_llp")
        if last_llp:
            self.page.run_task(self._auto_load_last_llp, Path(last_llp))

        # Runs regardless of whether preferences.json exists at all -
        # see _determine_hearing_quality_tier for why (first-ever
        # launch still needs a starting tier determined).
        self.hearing_quality_tier = self._determine_hearing_quality_tier(data.get("hearing_quality_tier"))

    async def _auto_load_last_llp(self, path: Path):
        """Reopens the last .llp that was loaded or saved, at startup"""
        if not path.exists():
            self._clear_stale_auto_load_state()
            return

        try:
            bundle = parse_llp_bundle(path.read_bytes())
        except (OSError, ValueError):
            self._clear_stale_auto_load_state()
            return

        await self._apply_loaded_llp_bundle(bundle, path.name, path, verb="Reopened")

    def _clear_stale_auto_load_state(self):
        """Called when the remembered last-opened .llp can't actually be reopened (deleted, moved, or corrupted)

        Also updates preferences.json immediately, not deferred to the next quit"""
        if self.my_selected_roles:
            self.my_selected_roles = set()
        self._save_user_preferences()
        self.build_menu()

    def _save_user_preferences(self):
        """Called right before the app actually closes - writes display mode and the currently loaded .llp's path"""
        data = {"dark_mode": self.dark_mode}

        if self.my_selected_roles:
            data["my_selected_roles"] = sorted(self.my_selected_roles)

        if self.loaded_script_path and self.loaded_script_path.suffix.lower() == ".llp":
            data["last_loaded_llp"] = str(self.loaded_script_path)

        if self.hearing_quality_tier:
            data["hearing_quality_tier"] = self.hearing_quality_tier

        try:
            self._preferences_file_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass  # preferences are a nicety - not worth failing the whole quit over

    # -----------------------------------------------------------------
    # Menu building
    # -----------------------------------------------------------------

    def show_menu(self):
        """Builds the initial screen. Later navigation just calls build_menu() again rather than re-running this whole setup."""
        self.build_menu()

    def build_menu(self):
        """Rebuilds the visible screen for the current self.menu_mode."""

        self.page.controls.clear()

        if self._app_initialising:
            # The very first thing that could ever be true, on a fresh
            # install - see _needs_initial_setup/__init__.
            self.page.add(self.build_initialising_screen())
            self.page.update()
            return

        if self._downloading_voices is not None:
            # A whole-screen state, not just a snackbar
            self.page.add(self.build_downloading_voices_screen())
            self.page.update()
            return

        if self._preparing_voices is not None:
            # Same reasoning as above, for ReadScriptAloud's pre-caching step
            self.page.add(self.build_preparing_voices_screen())
            self.page.update()
            return

        if self._read_aloud_active:
            # The live reading screen (see build_read_aloud_screen)
            self.page.add(ft.Container(content=self.build_read_aloud_screen(), expand=True))
            self.page.update()
            return

        header = ft.Column(
            [
                ft.Image(src=self.logo_image, width=96, height=96, fit=ft.BoxFit.CONTAIN),
                ft.Text("LineLearnerPro", size=28, weight=ft.FontWeight.W_500),
                ft.Text(
                    f"Script Loaded: {self.loaded_script_filename or 'none'}",
                    size=12,
                    italic=True,
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
        )

        if self.menu_mode == 0:
            body = self.build_main_menu()
        elif self.menu_mode == 1:
            body = self.build_script_tools_menu()
        elif self.menu_mode == 2:
            body = self.build_assign_photos_menu()
        elif self.menu_mode == 3:
            body = self.build_assign_voices_menu()
        elif self.menu_mode == 4:
            body = self.build_voice_picker_menu()
        elif self.menu_mode == 5:
            body = self.build_read_aloud_scope_menu()
        elif self.menu_mode == 6:
            body = self.build_select_my_roles_menu()
        elif self.menu_mode == 7:
            body = self.build_pronunciation_dictionary_menu()
        elif self.menu_mode == 8:
            body = self.build_pronunciation_edit_menu()
        elif self.menu_mode == 9:
            body = self.build_settings_menu()
        elif self.menu_mode == 10:
            body = self.build_hearing_quality_menu()
        elif self.menu_mode == 11:
            body = self.build_voice_manager_menu()
        elif self.menu_mode == 12:
            body = self.build_help_menu()
        elif self.menu_mode == 13:
            body = self.build_help_setup_menu()
        elif self.menu_mode == 14:
            body = self.build_help_rehearsing_menu()
        elif self.menu_mode == 15:
            body = self.build_help_settings_menu()
        else:
            body = self.build_help_about_menu()

        content = ft.Column(
            [
                ft.Container(height=24),
                header,
                ft.Container(height=24),
                body,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            scroll=ft.ScrollMode.ADAPTIVE,  # visible scrollbar on desktop/web, same reasoning as page.scroll
        )

        # Centred, width-capped container - this is what makes the same layout hold together on a phone-width column and a wide desktop
        self.page.add(
            ft.Container(
                content=content,
                alignment=ft.alignment.Alignment.TOP_CENTER,
                padding=ft.padding.Padding.symmetric(horizontal=16, vertical=16),
                expand=True,
            )
        )
        self.page.update()

    def build_initialising_screen(self) -> ft.Container:
        """Shown ONLY on a fresh install (or one otherwise missing its fallback voice / currently-selected Hearing Quality model)"""
        content = ft.Column(
            [
                ft.Container(height=24),
                ft.Image(src=self.logo_image, width=96, height=96, fit=ft.BoxFit.CONTAIN),
                ft.Text("Program Initialising", size=24, weight=ft.FontWeight.W_500),
                ft.Text("Please wait...", size=16),
                ft.ProgressRing(),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12,
        )
        return ft.Container(
            content=content,
            alignment=ft.alignment.Alignment.TOP_CENTER,
            padding=ft.padding.Padding.symmetric(horizontal=16, vertical=16),
            expand=True,
        )

    def build_voice_progress_screen(
        self, title: str, items: list, current_item: "str | None", label_fn=voice_label_for
    ) -> ft.Container:
        """Generic full-screen "working through a list, please wait" state"""
        total = len(items)
        if current_item in items:
            position = items.index(current_item) + 1
            progress_text = f"({position} of {total}) {label_fn(current_item)}"
        else:
            progress_text = ""

        content = ft.Column(
            [
                ft.Container(height=24),
                ft.Image(src=self.logo_image, width=96, height=96, fit=ft.BoxFit.CONTAIN),
                ft.Text(title, size=24, weight=ft.FontWeight.W_500),
                ft.Text("Please wait...", size=16),
                ft.ProgressRing(),
                ft.Text(progress_text, size=14, italic=True),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12,
        )
        return ft.Container(
            content=content,
            alignment=ft.alignment.Alignment.TOP_CENTER,
            padding=ft.padding.Padding.symmetric(horizontal=16, vertical=16),
            expand=True,
        )

    def build_downloading_voices_screen(self) -> ft.Container:
        """The full-screen state shown while LoadLLPScript is fetching any voices the loaded script needs that aren't on this device"""
        return self.build_voice_progress_screen(
            "Downloading Voices", self._downloading_voices, self._downloading_voices_current
        )

    def build_preparing_voices_screen(self) -> ft.Container:
        """The full-screen state shown while ReadScriptAloud is loading every voice a chosen scope needs into memory"""
        return self.build_voice_progress_screen(
            "Preparing Voices", self._preparing_voices, self._preparing_voices_current,
            label_fn=lambda character: character,
        )

    def build_main_menu(self) -> ft.Column:
        buttons = [
            self.menu_button("Settings and Help", self.open_settings, icon=ft.Icons.SETTINGS),
            self.menu_button("Script tools", self.open_script_tools, icon=ft.Icons.BUILD),
        ]

        buttons += [
            self.menu_button("Load LineLearnerPro script", self.load_llp_script, icon=ft.Icons.FOLDER_OPEN),
            self.menu_button(
                "Select my role(s)",
                self.select_my_roles,
                icon=ft.Icons.PERSON_SEARCH,
                disabled=not self.roles,
            ),
            self.menu_button(
                "Read script aloud",
                self.read_script_aloud,
                icon=ft.Icons.RECORD_VOICE_OVER,
                disabled=not self.parsed_script,
            ),
            self.menu_button(
                "Script read-along",
                self.script_read_along,
                icon=ft.Icons.SUBTITLES,
                disabled=not (self.my_selected_roles and self.parsed_script),
            ),
            self.menu_button(
                "Rehearse my lines",
                self.rehearse_my_lines,
                icon=ft.Icons.THEATER_COMEDY,
                disabled=not (self.my_selected_roles and self.parsed_script),
            ),
            self.menu_button("Quit LineLearnerPro", self.quit_llp, icon=ft.Icons.CLOSE, danger=True),
        ]

        return ft.Column(buttons, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def build_settings_menu(self) -> ft.Column:
        """A landing page for app-wide settings, not tied to any one script."""
        buttons = [
            ft.Text("Settings", size=20, weight=ft.FontWeight.W_500),
            self.menu_button("Light / Dark mode", self.switch_mode, icon=ft.Icons.CONTRAST),
            self.menu_button("Hearing quality", self.open_hearing_quality, icon=ft.Icons.HEARING),
            self.menu_button("Voice manager", self.open_voice_manager, icon=ft.Icons.RECORD_VOICE_OVER),
            self.menu_button("Pronunciation dictionary", self.open_pronunciation_dictionary, icon=ft.Icons.SPELLCHECK),
            self.menu_button("Help", self.open_help, icon=ft.Icons.HELP_OUTLINE),
            self.menu_button("Return to main menu", self.return_to_main_menu, icon=ft.Icons.ARROW_BACK),
        ]
        return ft.Column(buttons, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def build_help_menu(self) -> ft.Column:
        """A landing page for in-app help - four sections"""
        buttons = [
            ft.Text("Help", size=20, weight=ft.FontWeight.W_500),
            self.menu_button("What is LineLearnerPro?", self.open_help_about, icon=ft.Icons.INFO_OUTLINE),
            self.menu_button("Setting up a script", self.open_help_setup, icon=ft.Icons.DESCRIPTION),
            self.menu_button("Rehearsing", self.open_help_rehearsing, icon=ft.Icons.THEATER_COMEDY),
            self.menu_button("Settings", self.open_help_settings, icon=ft.Icons.SETTINGS),
            self.menu_button("Return to settings", self.return_to_settings, icon=ft.Icons.ARROW_BACK),
        ]
        return ft.Column(buttons, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def build_help_about_menu(self) -> ft.Column:
        """A friendly orientation for someone who's never used the app"""
        rows = [
            ft.Text("What is LineLearnerPro?", size=20, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "LineLearnerPro is a tool for learning lines away from "
                "rehearsals - not a replacement for them. It's built "
                "for the quiet, solitary work of getting a script into "
                "your head before you're back in a room with other "
                "actors, not for standing in as one."
            ),
            ft.Text("What it does", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Load a script and it reads every character's lines "
                "aloud using text-to-speech. Once you've picked your "
                "own role, it can pause on your lines and wait for you "
                "to speak - or just pause and wait for you to say "
                "you're ready - so you can practise your cues and "
                "recall in your own time, at your own pace."
            ),
            ft.Text("Why it's a bit different", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "The text-to-speech and speech-recognition voices are "
                "entirely free and run on your own machine - there's "
                "no subscription, no account, and no online AI service "
                "involved at all. Once a script's voices and the "
                "speech-recognition model are downloaded, the whole "
                "app works completely offline - nothing you read or "
                "say is ever sent anywhere."
            ),
            ft.Text("Worth knowing", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "The voices aren't context aware - they don't know "
                "when a line is a joke, a threat, or a whisper, and "
                "will read all three the same flat way. That makes "
                "them a poor substitute for a real actor's "
                "performance. It's a genuine limitation, and an honest "
                "trade-off for using free voices without relying on an "
                "online AI service - speech recognition has its own "
                "limits too, covered in the Rehearsing section."
            ),
            self._help_paragraph(
                "LineLearnerPro is, and always will be, free to use. "
                "If it's been useful to you, a small donation towards "
                "keeping it going is genuinely appreciated - but "
                "never expected."
            ),
            self.menu_button("Buy author a beer!", self.open_donation_link, icon=ft.Icons.SPORTS_BAR),
            self.menu_button("Return to help", self.return_to_help, icon=ft.Icons.ARROW_BACK),
        ]
        return ft.Column(rows, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _help_paragraph(self, text: str) -> ft.Text:
        """One consistently-styled paragraph of help prose"""
        return ft.Text(text, size=14, text_align=ft.TextAlign.LEFT, width=self.MAX_CONTENT_WIDTH)

    def _help_example_block(self, text: str) -> ft.Container:
        """A visually distinct block for a worked script example, set apart from the surrounding prose explanation."""
        return ft.Container(
            content=ft.Text(text, size=13, font_family="Courier New", selectable=True),
            width=self.MAX_CONTENT_WIDTH,
            padding=12,
            border=ft.Border.all(1, ft.Colors.OUTLINE),
            border_radius=8,
        )

    def build_help_setup_menu(self) -> ft.Column:
        """Documents the EXACT format parse_script_text expects"""
        rows = [
            ft.Text("Setting up a script", size=20, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Getting a script ready is five steps: write the .txt "
                "file in the format below, load it in, analyse it, "
                "optionally assign cast photos and voices, then save "
                "it as a .llp file. Once that's done, rehearsing with "
                "it is just a case of reopening that one file - see "
                "the Rehearsing section."
            ),
            self._help_paragraph(
                "If you chose to install the example files, there's an "
                "\"ExampleScript\" folder on your Desktop with a ready-"
                "made script, cast headshots, and its own short guide - "
                "a good way to try these five steps out before setting "
                "up a script of your own."
            ),
            ft.Text("1. Writing the .txt file", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "The app looks for specific patterns to tell dialogue, "
                "stage directions, and act/scene headings apart - "
                "getting these right matters, since anything that "
                "doesn't match won't be read aloud or assigned to a "
                "character."
            ),
            ft.Text("Dialogue", size=15, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Each line needs the character's name in UPPERCASE, "
                "followed by a colon, then what they say:"
            ),
            self._help_example_block("EVE: Hello there."),
            self._help_paragraph(
                "Numbers, apostrophes, and hyphens are fine in a name "
                "(SOLDIER 2:, O'BRIEN:, MOTHER-IN-LAW:) - but the name "
                "itself must be uppercase. A name in lowercase or "
                "mixed case (Eve:) won't be recognised as dialogue at "
                "all."
            ),
            ft.Text("Stage directions", size=15, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "A whole paragraph wrapped in parentheses, on its own, "
                "is a stage direction:"
            ),
            self._help_example_block("(The lights come up on an empty room.)"),
            self._help_paragraph(
                "A direction written inside a line of dialogue works "
                "the same way - it's shown separately and left out of "
                "the spoken audio, rather than read aloud as part of "
                "the line:"
            ),
            self._help_example_block("EVE: Get away from here! (she storms off)"),
            ft.Text("Acts and scenes", size=15, weight=ft.FontWeight.W_500),
            self._help_paragraph("Wrap a heading in square brackets, on its own:"),
            self._help_example_block("[Act 1]\n[Scene 2]\n[Introduction]"),
            self._help_paragraph(
                'A heading starting with the word "Act" opens a new '
                "act; anything else opens a new scene within whichever "
                "act you're currently in. Neither is required - a "
                "short, single-scene script can skip headings "
                "entirely, and everything falls under one implicit "
                "act and scene."
            ),
            ft.Text("Keeping things apart", size=15, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Leave at least one blank line between everything - "
                "each heading, direction, and line of dialogue needs "
                "its own paragraph, with a blank line above and below."
            ),
            ft.Text("A complete example", size=15, weight=ft.FontWeight.W_500),
            self._help_example_block(
                "[Act 1]\n\n"
                "[Scene 1]\n\n"
                "(The lights come up on an empty kitchen.)\n\n"
                "EVE: Adam, where are you?\n\n"
                "ADAM: In here! (calling from offstage) I'm feeding the cat.\n\n"
                "EVE: You don't have a cat."
            ),
            self._help_paragraph(
                "Anything that doesn't match one of the patterns above "
                "is kept, not thrown away - but it won't be read aloud "
                "or assigned to a role. If a character's lines aren't "
                "showing up after Analyse Script, the most common "
                "cause is a name that isn't fully uppercase."
            ),
            ft.Text("2. Loading it in", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                'In Script Tools: "Load txt script" opens a file '
                "picker restricted to .txt files. This just reads the "
                "text into the app - nothing is understood about its "
                "structure yet, that's the next step."
            ),
            ft.Text("3. Analysing the script", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                '"Analyse script" is what actually reads through the '
                "text and works out the cast list, acts, and scenes - "
                "nothing else in the app knows any of that until this "
                "step runs. Safe to run again any time after editing "
                "the .txt (or the pronunciation dictionary) - it keeps "
                "whatever photo or voice you'd already assigned for "
                "any character name it still recognises."
            ),
            ft.Text("Starting a genuinely new project", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Loading a different .txt over an existing project "
                "deliberately keeps whatever's already been set up - "
                "matching characters keep their assigned photo and "
                "voice, exactly as described above. That's what makes "
                "editing a line and reloading work smoothly, without "
                "redoing everything from scratch."
            ),
            self._help_paragraph(
                "But it also means the app has no reliable way to tell "
                '"an edited version of the same play" apart from "a '
                'genuinely different one" - both just look like a .txt '
                "file being loaded, and a filename can't be trusted to "
                "tell them apart either (nothing stops a script being "
                'renamed "final version 2.txt" after an edit). If '
                'you\'re starting a completely new, unrelated project, '
                'use "Remove loaded script", at the top of Script '
                "Tools, first - it clears the current script, its "
                "analysis, and every role's assignments, so the new "
                "one starts genuinely fresh. It's greyed out until "
                "something's actually loaded, and if there's unsaved "
                "work at the time, it asks whether to save first."
            ),
            ft.Text("4. Cast photos and voices (optional)", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                'Also in Script Tools: "Assign cast photos" and '
                '"Assign voices" let you put a face and a text-to-'
                "speech voice to each character, used throughout "
                "reading and rehearsing. Entirely optional - a role "
                "with nothing assigned still works, it just uses a "
                'plain icon and the app\'s single, built-in fallback '
                'voice ("Alan", a British voice) to read that '
                "character's lines instead."
            ),
            self._help_paragraph(
                "Assigning a voice to a role also sets that role's "
                "gender, which filters which voices are offered - only "
                "male voices for a male role, only female for a female "
                "one. With no gender set yet, every voice is offered "
                "instead, and the app says so on screen at the time."
            ),
            self._help_paragraph(
                "See the Settings section of this Help for Hearing "
                "Quality and Voice Manager, which affect voices more "
                "broadly."
            ),
            ft.Text("5. Saving as a .llp file", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                '"Save LineLearnerPro script" bundles everything - the '
                "analysed script, cast photos, and voice assignments - "
                "into a single .llp file. This step matters: nothing "
                "about a script survives closing the app unless it's "
                "been saved this way - a script that's only been "
                "loaded and analysed, but never saved, is gone the "
                "moment the app closes. A saved .llp is also all "
                "another person needs - handing them this one file "
                "gives them the fully prepared script, cast photos and "
                "voices included, without redoing any of this setup "
                "themselves."
            ),
            self.menu_button("Return to help", self.return_to_help, icon=ft.Icons.ARROW_BACK),
        ]
        return ft.Column(rows, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def build_help_rehearsing_menu(self) -> ft.Column:
        """Explains what's needed before rehearsing, then the three reading modes"""
        rows = [
            ft.Text("Rehearsing", size=20, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Rehearsing needs a script that's already been set up - "
                "see \"Setting up a script\" if you haven't got a .llp "
                "file ready yet."
            ),
            ft.Text("Loading a prepared script", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                '"Load LineLearnerPro script" opens a .llp file - the '
                "cast, photos, and voices all come with it, exactly as "
                "they were when it was saved. The app also reopens "
                "whichever .llp you had open last, automatically, the "
                "next time it starts - so once a script's set up, "
                "picking up where you left off is just opening the app "
                "again."
            ),
            ft.Text("Selecting your role(s)", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                '"Select my role(s)", from the main menu, comes next - '
                "Script read-along and Rehearse my lines are both "
                "about YOUR lines specifically, and stay unavailable "
                "until at least one role is chosen. Once you're "
                "picking which act or scene to run, anything with none "
                "of your lines in it is greyed out too, since there'd "
                "be nothing for either mode to actually pause on."
            ),
            self._help_paragraph("With a role selected, there are three ways to work with a script:"),
            ft.Text("Read script aloud", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Every character's lines are read by the app, back to "
                "back, with no pauses. Doesn't need a role selected - "
                "good for hearing how a whole scene sounds, or for "
                "following along on the page."
            ),
            ft.Text("Script read-along", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "The app reads everyone else's lines as normal, but "
                "pauses on yours and waits for you to speak before "
                "continuing. Good for learning your cues - when to "
                "come in, and off what line."
            ),
            ft.Text("Rehearse my lines", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Works like Script read-along, but also listens to "
                "what you actually say and flags - in red - any line "
                "that didn't sound close enough to the script. This is "
                "advisory only: speech recognition accuracy varies "
                "with hardware and microphone quality, so a flagged "
                "line isn't necessarily wrong, just worth a glance. "
                "Nothing is ever blocked by a flag - the read-through "
                "always carries on regardless."
            ),
            self._help_paragraph(
                "One thing worth knowing plainly: a strong accent, of "
                "any kind, will cause the speech recognition to "
                "struggle more often, and there's no real way around "
                "this - it's an unavoidable limit of the underlying "
                "technology, not something a setting can fix."
            ),
            ft.Text("Smart Cue", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                f"Available in all three reading modes. Jumps reading "
                f"ahead to about {SMART_CUE_TARGET_SECONDS} seconds "
                f"before your own next line, so there's no need to sit "
                f"through a long stretch of someone else's dialogue "
                f"just to reach your cue."
            ),
            self._help_paragraph(
                "Never cuts a line off partway through - if landing "
                "early would fall in the middle of a line, that whole "
                "line is read from its start instead, even if that "
                "means slightly more lead-in than usual."
            ),
            self._help_paragraph(
                "What gets skipped is still shown, briefly, dimmed in "
                "the transcript as it flashes past - not silently "
                "missed out, just skipped through quickly rather than "
                "performed."
            ),
            self._help_paragraph(
                "Greyed out and unavailable whenever there's nothing "
                "useful to jump to - already close to your next line, "
                "or no more of your own lines left in the current "
                "scope."
            ),
            ft.Text("Keyboard shortcuts (desktop)", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "On a desktop machine, Esc goes back a menu level from "
                "almost anywhere in the app - handy on a long list "
                "like Voice Manager, without scrolling all the way "
                "down to a Return button. During an active reading "
                "screen, Esc quits back to the menu instead."
            ),
            self._help_paragraph(
                "In Script read-along and Rehearse my lines "
                "specifically: any OTHER key - not just Esc - manually "
                "advances past your own line, whether or not you've "
                "actually said anything. Useful if a microphone isn't "
                "picking you up, you're deliberately mumbling or "
                "whispering through a line, or there's simply no "
                "microphone available at all - you're never stuck "
                "waiting on speech recognition to work."
            ),
            self._help_paragraph(
                "Voices and speech recognition are both covered in the "
                "Settings section of this Help."
            ),
            self.menu_button("Return to help", self.return_to_help, icon=ft.Icons.ARROW_BACK),
        ]
        return ft.Column(rows, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def build_help_settings_menu(self) -> ft.Column:
        """Covers everything in the Settings menu"""
        rows = [
            ft.Text("Settings", size=20, weight=ft.FontWeight.W_500),
            ft.Text("Light / Dark mode", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph("Switches the app between light and dark colour schemes."),
            ft.Text("Hearing quality", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Controls which speech-recognition model Script read-"
                "along and Rehearse my lines use to auto-advance when "
                "you finish speaking. Low, Medium, and High trade "
                "accuracy against how much they ask of your hardware - "
                "a higher tier tends to recognise speech more "
                "accurately, but can run poorly on older machines, so "
                "it's worth trying a lower tier first rather than "
                "assuming higher is always better."
            ),
            self._help_paragraph(
                "Each tier can be downloaded and deleted independently "
                "- a tier that isn't currently selected is always safe "
                "to delete to free up disk space, and can simply be "
                "downloaded again later if wanted back. The tier "
                "currently in use can't be deleted until a different "
                "one is selected instead."
            ),
            ft.Text("Voice manager", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Lists every available text-to-speech voice, with "
                "Download, Preview, and Delete for each - independent "
                "of assigning voices to characters in Script Tools, "
                "which just picks from whatever's available here."
            ),
            self._help_paragraph(
                "A downloaded voice that isn't currently assigned to "
                "any role, and isn't \"Alan\" (the single, built-in "
                "voice used for any role with nothing assigned - see "
                "\"Setting up a script\"), is always safe to delete to "
                "free up disk space - downloading it again later "
                "brings it straight back. A voice actively assigned to "
                "a role, or Alan itself, can't be deleted until it's "
                "no longer needed, so there's no risk of accidentally "
                "removing one still in use."
            ),
            ft.Text("Pronunciation dictionary", size=16, weight=ft.FontWeight.W_500),
            self._help_paragraph(
                "Fixes words the text-to-speech voices mispronounce, "
                'by replacing a word with a phonetic respelling '
                'wherever it appears. "Add new word" asks for the '
                'Word as it\'s spelled in the script, and a Phonetic '
                'spelling to replace it with when read aloud - "Say '
                'This" previews how the replacement actually sounds '
                "before saving. Takes effect immediately across the "
                "whole script, not just newly typed text."
            ),
            self._help_paragraph(
                "A common reason to need this: regional pronunciation. "
                "A British voice, for instance, may say \"Uranus\" in "
                "a way that doesn't land the way it's meant to - "
                "adding an entry with the phonetic spelling "
                '"Uraynus" fixes how that voice specifically says it, '
                "regardless of why the original pronunciation wasn't "
                "right."
            ),
            self._help_paragraph(
                "The phonetic spelling doesn't have to be one word - "
                'replacing "Alphonso" with "Al Fonso" is entirely '
                "valid, and can be a simpler way to nudge a name "
                "toward the right sound than inventing a single "
                "unusual spelling."
            ),
            self._help_paragraph(
                "A genuine limitation: the dictionary replaces every "
                "occurrence of a word the same way, with no awareness "
                "of which meaning was intended - so it can't handle "
                "heteronyms (words spelled identically but pronounced "
                'differently depending on meaning, like "read" - '
                '"reed" in "I read books", "red" in "I read that '
                'book yesterday"). Adding an entry to fix one meaning\'s '
                "pronunciation will apply that same fix to every other "
                "occurrence of that spelling too, correct or not - "
                "worth checking a script for a word used both ways "
                "before relying on this to fix just one of them."
            ),
            self.menu_button("Return to help", self.return_to_help, icon=ft.Icons.ARROW_BACK),
        ]
        return ft.Column(rows, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def build_hearing_quality_menu(self) -> ft.Column:
        """The user-facing name for choosing which Vosk speech-recognition model to use"""
        rows = [
            ft.Text("Hearing quality", size=20, weight=ft.FontWeight.W_500),
            ft.Text(
                "Controls how accurately the app recognizes your speech "
                "in Rehearse My Lines and Script Read-along. Higher "
                "quality needs a bigger download and more from your "
                "hardware to run smoothly - download and select "
                "whichever suits your machine.",
                size=12,
                italic=True,
                text_align=ft.TextAlign.CENTER,
            ),
        ]
        for tier_entry in HEARING_QUALITY_TIERS:
            rows.append(self._build_hearing_quality_tier_row(tier_entry))
        rows.append(self.menu_button("Return to settings", self.return_to_settings, icon=ft.Icons.ARROW_BACK))
        return ft.Column(rows, spacing=16, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _build_hearing_quality_tier_row(self, tier_entry: dict) -> ft.Container:
        """One selectable Hearing Quality tier: its label/size/description, and Download/Downloaded, Select/Selected, and Delete lozenges"""
        tier = tier_entry["tier"]
        model_name = tier_entry["model_name"]
        downloaded = is_vosk_model_downloaded(model_name)
        is_selected = self.hearing_quality_tier == tier
        is_downloading = model_name in self._hearing_quality_downloading

        download_button = ft.Button(
            "Downloading..." if is_downloading else ("Downloaded" if downloaded else "Download"),
            on_click=None if (is_downloading or downloaded) else self._make_download_hearing_quality_handler(tier_entry),
            disabled=is_downloading or downloaded,
            height=self.BUTTON_HEIGHT,
        )

        # Select stays disabled until Download has genuinely finished
        select_button = ft.Button(
            "Selected" if is_selected else "Select",
            on_click=None if (is_selected or not downloaded) else self._make_select_hearing_quality_handler(tier),
            disabled=is_selected or not downloaded,
            height=self.BUTTON_HEIGHT,
            bgcolor=ft.Colors.PRIMARY if is_selected else None,
            color=ft.Colors.ON_PRIMARY if is_selected else None,
        )

        # Disabled for the currently selected tier too, not just when nothing's downloaded
        can_delete = downloaded and not is_selected
        delete_button = ft.Button(
            "Delete",
            on_click=self._make_delete_hearing_quality_handler(tier_entry) if can_delete else None,
            disabled=not can_delete,
            height=self.BUTTON_HEIGHT,
            bgcolor=ft.Colors.ERROR if can_delete else None,
            color=ft.Colors.ON_ERROR if can_delete else None,
        )

        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(f"{tier_entry['label']} ({tier_entry['size_mb']}MB)", size=16, weight=ft.FontWeight.W_500),
                    ft.Text(tier_entry["description"], size=12, italic=True, text_align=ft.TextAlign.CENTER),
                    ft.Row([download_button, select_button, delete_button], spacing=10, alignment=ft.MainAxisAlignment.CENTER),
                ],
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=self._role_row_width(),
            padding=12,
        )

    def _make_download_hearing_quality_handler(self, tier_entry: dict):
        """Factory, same late-binding-closure reason as elsewhere."""
        async def handler(e):
            model_name = tier_entry["model_name"]
            self._hearing_quality_downloading.add(model_name)
            self.build_menu()

            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, download_vosk_model_sync, model_name)
            except Exception as ex:
                self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't download this hearing quality tier: {ex}")))
            finally:
                self._hearing_quality_downloading.discard(model_name)
            self.build_menu()
        return handler

    def _make_select_hearing_quality_handler(self, tier: str):
        """Factory, same late-binding-closure reason as elsewhere. No manual cache-clearing needed here"""
        def handler(e):
            self.hearing_quality_tier = tier
            self.build_menu()
        return handler

    def _make_delete_hearing_quality_handler(self, tier_entry: dict):
        """Factory, same late-binding-closure reason as elsewhere"""
        async def handler(e):
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, delete_vosk_model_sync, tier_entry["model_name"])
            self.build_menu()
        return handler

    def build_voice_manager_menu(self) -> ft.Column:
        """Lists EVERY curated voice (unlike the per-role "Select voice" screen, which filters by the role's gender)"""
        rows = [
            ft.Text("Voice manager", size=20, weight=ft.FontWeight.W_500),
            ft.Text(
                "Download, preview, or remove any of the available "
                "voices. A voice currently assigned to a role in the "
                "loaded script - or the built-in fallback voice - can't "
                "be deleted until it's no longer needed.",
                size=12,
                italic=True,
                text_align=ft.TextAlign.CENTER,
            ),
        ]
        for voice in CURATED_VOICES:
            rows.append(self._build_voice_manager_row(voice))
        rows.append(self.menu_button("Return to settings", self.return_to_settings, icon=ft.Icons.ARROW_BACK))
        return ft.Column(rows, spacing=16, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _build_voice_manager_row(self, voice: dict) -> ft.Container:
        """One voice's row: label, and Download/Downloaded, Preview, and Delete lozenges"""
        voice_id = voice["id"]
        downloaded = is_voice_downloaded(voice_id)
        is_loading = voice_id in self._voices_currently_loading
        in_use_by_role = any(role.voice == voice_id for role in self.roles)
        is_fallback = voice_id == FALLBACK_VOICE_ID
        can_delete = downloaded and not is_loading and not in_use_by_role and not is_fallback

        if is_fallback:
            status_note = " - built-in fallback voice"
        elif in_use_by_role:
            status_note = " - in use in the loaded script"
        else:
            status_note = ""

        download_button = ft.Button(
            "Downloading..." if is_loading else ("Downloaded" if downloaded else "Download"),
            on_click=None if (is_loading or downloaded) else self._make_download_voice_manager_handler(voice_id),
            disabled=is_loading or downloaded,
            height=self.BUTTON_HEIGHT,
        )

        preview_button = ft.Button(
            "Downloading..." if is_loading else "Preview",
            on_click=None if is_loading else self._make_preview_voice_handler(voice_id),
            disabled=is_loading,
            height=self.BUTTON_HEIGHT,
        )

        delete_button = ft.Button(
            "Delete",
            on_click=self._make_delete_voice_manager_handler(voice_id) if can_delete else None,
            disabled=not can_delete,
            height=self.BUTTON_HEIGHT,
            bgcolor=ft.Colors.ERROR if can_delete else None,
            color=ft.Colors.ON_ERROR if can_delete else None,
        )

        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        f"{voice['label']} - {voice['gender'].capitalize()}{status_note}",
                        size=16,
                        weight=ft.FontWeight.W_500,
                    ),
                    ft.Row([download_button, preview_button, delete_button], spacing=10, alignment=ft.MainAxisAlignment.CENTER),
                ],
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=self._role_row_width(),
            padding=12,
        )

    def _make_download_voice_manager_handler(self, voice_id: str):
        """Factory, same late-binding-closure reason as elsewhere"""
        async def handler(e):
            self._voices_currently_loading.add(voice_id)
            self.build_menu()

            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, download_voice_and_ensure_audition_sync, voice_id)
            except Exception as ex:
                self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't download this voice: {ex}")))
            finally:
                self._voices_currently_loading.discard(voice_id)
            self.build_menu()
        return handler

    def _make_delete_voice_manager_handler(self, voice_id: str):
        """Factory, same late-binding-closure reason as elsewhere"""
        async def handler(e):
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, delete_voice_sync, voice_id)
            self.build_menu()
        return handler

    def build_script_tools_menu(self) -> ft.Column:
        buttons = [
            ft.Text("Script tools", size=20, weight=ft.FontWeight.W_500),
            self.menu_button(
                "Remove loaded script",
                self.remove_loaded_script,
                icon=ft.Icons.DELETE_OUTLINE,
                disabled=not (self.raw_script_text or self.parsed_script),
            ),
            self.menu_button("Load txt script", self.load_txt_script, icon=ft.Icons.DESCRIPTION),
            self.menu_button("Load LineLearnerPro script", self.load_llp_script, icon=ft.Icons.FOLDER_OPEN),
            self.menu_button(
                "Analyse script",
                self.analyse_script,
                icon=ft.Icons.MANAGE_SEARCH,
                disabled=not self.raw_script_text,
            ),
            self.menu_button(
                "Assign cast photos",
                self.assign_photos,
                icon=ft.Icons.PHOTO_CAMERA,
                disabled=not self.parsed_script,
            ),
            self.menu_button(
                "Assign voices",
                self.assign_voices,
                icon=ft.Icons.RECORD_VOICE_OVER,
                disabled=not self.parsed_script,
            ),
            self.menu_button(
                "Save LineLearnerPro script",
                self.save_llp_script,
                icon=ft.Icons.SAVE,
                disabled=not self.parsed_script,
            ),
            self.menu_button("Return to main menu", self.return_to_main_menu, icon=ft.Icons.ARROW_BACK),
        ]
        return ft.Column(buttons, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def build_assign_photos_menu(self) -> ft.Column:
        rows = [ft.Text("Assign cast photos", size=20, weight=ft.FontWeight.W_500)]

        if not self.roles:
            rows.append(
                ft.Text(
                    "Analyse a script first - there are no roles to assign photos to yet.",
                    size=14,
                    text_align=ft.TextAlign.CENTER,
                )
            )
        else:
            for role in self.roles:
                rows.append(self._build_role_photo_row(role))

        rows.append(self.menu_button("Return to script tools", self.return_to_script_tools, icon=ft.Icons.ARROW_BACK))

        return ft.Column(rows, spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _mark_dirty(self):
        """Flags the current project as having changes not yet reflected in a saved .llp"""
        self.has_unsaved_changes = True

    def _mark_saved(self, filename: str, path: "Path | None"):
        """Called after ANY successful save"""
        self.loaded_script_filename = filename
        self.loaded_script_path = path
        self.raw_script_text = None  # no longer meaningful once it's an .llp - matches LoadLLPScript
        self.has_unsaved_changes = False
        self.build_menu()

    def _default_icon_source(self, role: "Role") -> str:
        """Which built-in image stands in for a role's photo when none has been assigned"""
        if role.gender == "male":
            return self.default_photo_a  # blue
        elif role.gender == "female":
            return self.default_photo_b  # pink
        return self.logo_image  # neutral - gender not chosen yet

    def _role_row_width(self) -> int:
        """How wide to make each role's photo-assignment row."""
        available = self.page.width or 900
        return int(max(280, min(700, available - 32)))

    def _build_role_photo_row(self, role: "Role") -> ft.Container:
        """One row per role - thumbnail, name, gender toggle and the choose/change-photo button all on one line where possible"""
        if role.photo:
            image = ft.Image(src=role.photo, width=96, height=96, fit=ft.BoxFit.CONTAIN)
        else:
            image = ft.Image(src=self._default_icon_source(role), width=96, height=96, fit=ft.BoxFit.CONTAIN)

        return ft.Container(
            content=ft.Row(
                [
                    image,
                    ft.Text(role.character, size=16, expand=True),
                    self._gender_button(role, "male", "Male"),
                    self._gender_button(role, "female", "Female"),
                    ft.Button(
                        "Change photo" if role.photo else "Choose photo",
                        on_click=self._make_assign_photo_handler(role),
                        height=self.BUTTON_HEIGHT,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=self._role_row_width(),
            padding=ft.padding.Padding.symmetric(horizontal=8, vertical=4),
        )

    def build_assign_voices_menu(self) -> ft.Column:
        """GLBasic: FUNCTION AssignVoices - the screen half of it. Lists one row per role showing its currently assigned voice"""
        rows = [ft.Text("Assign voices", size=20, weight=ft.FontWeight.W_500)]

        if not self.roles:
            rows.append(
                ft.Text(
                    "Analyse a script first - there are no roles to assign voices to yet.",
                    size=14,
                    text_align=ft.TextAlign.CENTER,
                )
            )
        else:
            for role in self.roles:
                rows.append(self._build_role_voice_row(role))

        rows.append(self.menu_button("Return to script tools", self.return_to_script_tools, icon=ft.Icons.ARROW_BACK))

        return ft.Column(rows, spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _build_role_voice_row(self, role: "Role") -> ft.Container:
        """One row per role - thumbnail, name + current voice (or "No voice assigned"), and a Choose/Change voice button."""
        if role.photo:
            image = ft.Image(src=role.photo, width=96, height=96, fit=ft.BoxFit.CONTAIN)
        else:
            image = ft.Image(src=self._default_icon_source(role), width=96, height=96, fit=ft.BoxFit.CONTAIN)

        voice_text = voice_label_for(role.voice) if role.voice else "No voice assigned"

        return ft.Container(
            content=ft.Row(
                [
                    image,
                    ft.Column(
                        [
                            ft.Text(role.character, size=16),
                            ft.Text(voice_text, size=12, italic=True),
                        ],
                        spacing=0,
                        expand=True,
                    ),
                    ft.Button(
                        "Change voice" if role.voice else "Choose voice",
                        on_click=self._make_open_voice_picker_handler(role),
                        height=self.BUTTON_HEIGHT,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=self._role_row_width(),
            padding=ft.padding.Padding.symmetric(horizontal=8, vertical=4),
        )

    def _make_open_voice_picker_handler(self, role: "Role"):
        """Factory, same late-binding-closure reason as the other _make_*_handler methods in this class."""
        def handler(e):
            self._voice_picker_role = role
            self.menu_mode = 4
            self.build_menu()
        return handler

    def build_voice_picker_menu(self) -> ft.Column:
        """Shows only the curated voices matching the role's gender if set in Assign cast photos - otherwise shows the FULL curated list"""
        role = self._voice_picker_role

        if role.photo:
            role_image = ft.Image(src=role.photo, width=96, height=96, fit=ft.BoxFit.CONTAIN)
        else:
            role_image = ft.Image(src=self._default_icon_source(role), width=96, height=96, fit=ft.BoxFit.CONTAIN)

        rows = [
            ft.Row(
                [role_image, ft.Text(f"Choose a voice for {role.character}", size=20, weight=ft.FontWeight.W_500)],
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=10,
            )
        ]

        if role.gender in ("male", "female"):
            voices = [v for v in CURATED_VOICES if v["gender"] == role.gender]
        else:
            voices = CURATED_VOICES
            rows.append(
                ft.Text(
                    "No gender set for this role, so every voice is shown. "
                    "Set a gender in Assign cast photos to narrow this list down.",
                    size=12,
                    italic=True,
                    text_align=ft.TextAlign.CENTER,
                )
            )

        for voice in voices:
            rows.append(self._build_voice_option_row(role, voice))

        rows.append(self.menu_button("Return to assign voices", self.return_to_assign_voices, icon=ft.Icons.ARROW_BACK))

        return ft.Column(rows, spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _build_voice_option_row(self, role: "Role", voice: dict) -> ft.Container:
        """One selectable voice option: its label, a Preview button, and a Select button that's highlighted when it's chosen"""
        selected = role.voice == voice["id"]
        show_gender_tag = role.gender not in ("male", "female")
        label = voice["label"] + (f" - {voice['gender'].capitalize()}" if show_gender_tag else "")
        is_loading = voice["id"] in self._voices_currently_loading

        return ft.Container(
            content=ft.Row(
                [
                    ft.Text(label, size=16, expand=True),
                    ft.Button(
                        "Downloading..." if is_loading else "Preview",
                        on_click=None if is_loading else self._make_preview_voice_handler(voice["id"]),
                        disabled=is_loading,
                        height=self.BUTTON_HEIGHT,
                    ),
                    ft.Button(
                        "Downloading..." if is_loading else ("Selected" if selected else "Select"),
                        on_click=None if is_loading else self._make_select_voice_handler(role, voice["id"]),
                        disabled=is_loading,
                        height=self.BUTTON_HEIGHT,
                        bgcolor=ft.Colors.PRIMARY if selected else None,
                        color=ft.Colors.ON_PRIMARY if selected else None,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=self._role_row_width(),
            padding=ft.padding.Padding.symmetric(horizontal=8, vertical=4),
        )

    def _make_select_voice_handler(self, role: "Role", voice_id: str):
        """Factory, same late-binding-closure reason as elsewhere"""
        async def handler(e):
            if not is_voice_downloaded(voice_id):
                self._voices_currently_loading.add(voice_id)
                self.build_menu()

                loop = asyncio.get_event_loop()
                try:
                    # download_voice_and_ensure_audition_sync
                    await loop.run_in_executor(None, download_voice_and_ensure_audition_sync, voice_id)
                except Exception as ex:
                    self._voices_currently_loading.discard(voice_id)
                    self.page.show_dialog(ft.SnackBar(
                        ft.Text(f"Couldn't download this voice: {ex} - assigned anyway, will retry when needed.")
                    ))
                    self.build_menu()
                else:
                    self._voices_currently_loading.discard(voice_id)

            role.voice = voice_id
            self._mark_dirty()
            self.build_menu()  # refresh so the "Selected" highlight updates immediately
        return handler

    def _make_preview_voice_handler(self, voice_id: str):
        async def handler(e):
            await self._preview_voice(voice_id)
        return handler

    async def _preview_voice(self, voice_id: str):
        """Plays the audition clip for one voice"""
        self._voices_currently_loading.add(voice_id)
        self.build_menu()  # button for this voice now shows "Downloading..." and is disabled

        loop = asyncio.get_event_loop()
        try:
            clip_path = await loop.run_in_executor(None, ensure_audition_clip_sync, voice_id)
        except Exception as ex:
            self._voices_currently_loading.discard(voice_id)
            self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't prepare this voice: {ex}")))
            self.build_menu()
            return

        self._voices_currently_loading.discard(voice_id)
        self.build_menu()  # button back to normal "Preview" before playback starts

        try:
            if self.is_desktop:
                await loop.run_in_executor(None, play_wav_file_sync, clip_path)
            else:
                self.voice_player.src = str(clip_path)
                await self.voice_player.play()
        except Exception as ex:
            self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't play preview: {ex}")))

    def _gender_button(self, role: "Role", value: str, label: str) -> ft.Button:
        """One half of the Male/Female toggle. Neither option is ever mandatory"""
        selected = role.gender == value
        return ft.Button(
            label,
            on_click=self._make_gender_handler(role, value),
            bgcolor=ft.Colors.PRIMARY if selected else None,
            color=ft.Colors.ON_PRIMARY if selected else None,
            height=36,
        )

    def _make_gender_handler(self, role: "Role", value: str):
        """Factory, same late-binding-closure reason as _make_assign_photo_handler below"""
        def handler(e):
            role.gender = value
            self._mark_dirty()
            self.build_menu()  # refresh so the default icon updates immediately if no custom photo is set
        return handler

    def _make_assign_photo_handler(self, role: "Role"):
        """Factory returning a fresh async click handler bound to THIS role."""
        async def handler(e):
            await self._assign_photo_for_role(role)
        return handler

    def menu_button(self, label: str, on_click, icon=None, danger: bool = False, disabled: bool = False) -> ft.Button:
        """One place that defines what every menu control looks like, so touch-target size and styling stay consistent everywhere"""
        apply_danger_style = danger and not disabled
        return ft.Button(
            content=ft.Row(
                [ft.Icon(icon), ft.Text(label, size=16)] if icon else [ft.Text(label, size=16)],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
            ),
            on_click=on_click,
            height=self.BUTTON_HEIGHT,
            width=self.MAX_CONTENT_WIDTH,
            color=ft.Colors.ON_ERROR if apply_danger_style else None,
            bgcolor=ft.Colors.ERROR if apply_danger_style else None,
            disabled=disabled,
        )

    # -----------------------------------------------------------------
    # Menu navigation
    # -----------------------------------------------------------------

    def open_script_tools(self, e):
        self.menu_mode = 1
        self.build_menu()

    def open_settings(self, e):
        self.menu_mode = 9
        self.build_menu()

    def open_hearing_quality(self, e):
        self.menu_mode = 10
        self.build_menu()

    def open_voice_manager(self, e):
        self.menu_mode = 11
        self.build_menu()

    def open_help(self, e):
        self.menu_mode = 12
        self.build_menu()

    def open_help_about(self, e):
        self.menu_mode = 16
        self.build_menu()

    def open_donation_link(self, e):
        """Opens DONATION_URL in the system's default browser. Uses Python's own webbrowser module directly on desktop"""
        opened = False
        if self.is_desktop:
            try:
                opened = bool(webbrowser.open(DONATION_URL))
            except Exception:
                opened = False
        else:
            try:
                self.page.launch_url(DONATION_URL)
                opened = True
            except Exception:
                opened = False

        if opened:
            self.page.show_dialog(ft.SnackBar(ft.Text("Opening your browser...")))
        else:
            self.page.show_dialog(ft.SnackBar(ft.Text(
                f"Couldn't open a browser automatically - you can visit {DONATION_URL} directly."
            )))

    def open_help_setup(self, e):
        self.menu_mode = 13
        self.build_menu()

    def open_help_rehearsing(self, e):
        self.menu_mode = 14
        self.build_menu()

    def open_help_settings(self, e):
        self.menu_mode = 15
        self.build_menu()

    def return_to_help(self, e):
        self.menu_mode = 12
        self.build_menu()

    def return_to_main_menu(self, e):
        self.menu_mode = 0
        self.build_menu()

    def return_to_script_tools(self, e):
        self.menu_mode = 1
        self.build_menu()

    def return_to_settings(self, e):
        self.menu_mode = 9
        self.build_menu()

    def return_to_assign_voices(self, e):
        self.menu_mode = 3
        self.build_menu()

    def switch_mode(self, e):
        """GLBasic: FUNCTION SwitchMode (Light/Dark mode)"""
        self.dark_mode = not self.dark_mode
        self.page.theme_mode = ft.ThemeMode.DARK if self.dark_mode else ft.ThemeMode.LIGHT
        self.page.bgcolor = "#000040" if self.dark_mode else "#E0E0E0"
        self.build_menu()

    async def quit_llp(self, e):
        """If there are unsaved changes, prompts before actually closing"""
        if self.has_unsaved_changes:
            self.page.show_dialog(self._build_unsaved_changes_dialog())
            return
        await self._quit_now()

    async def _quit_now(self):
        """The final quit step"""
        self._save_user_preferences()
        await self.page.window.close()

    def _build_unsaved_changes_dialog(self) -> ft.AlertDialog:
        """The Save / Discard / Cancel prompt shown by QuitLLP when has_unsaved_changes is True."""

        def cancel(e):
            self.page.pop_dialog()

        async def discard_and_quit(e):
            self.page.pop_dialog()
            await self._quit_now()

        async def save_and_quit(e):
            self.page.pop_dialog()
            await self.save_llp_script(None)
            if not self.has_unsaved_changes:  # save genuinely succeeded
                await self._quit_now()

        return ft.AlertDialog(
            modal=True,
            title=ft.Text("Unsaved changes"),
            content=ft.Text("This project has changes that haven't been saved. Save before quitting?"),
            actions=[
                ft.Button("Cancel", on_click=cancel),
                ft.Button("Discard & Quit", on_click=discard_and_quit),
                ft.Button("Save & Quit", on_click=save_and_quit),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

    def remove_loaded_script(self, e):
        """Clears everything about the currently loaded project - raw text, the analysed structure, every role's assignments and
        selections, and the remembered file path"""
        if self.has_unsaved_changes:
            self.page.show_dialog(self._build_discard_project_dialog())
            return
        self._do_remove_loaded_script()

    def _do_remove_loaded_script(self):
        self.raw_script_text = None
        self.parsed_script = None
        self.roles = []
        self.my_selected_roles = set()
        self.has_unsaved_changes = False
        self.loaded_script_filename = None
        self.loaded_script_path = None
        # Written immediately, not deferred to the next quit
        self._save_user_preferences()
        self.build_menu()
        self.page.show_dialog(ft.SnackBar(ft.Text("Script removed - ready to load a new one.")))

    def _build_discard_project_dialog(self) -> ft.AlertDialog:
        """The Save / Discard / Cancel prompt shown by RemoveLoadedScript when has_unsaved_changes is True"""

        def cancel(e):
            self.page.pop_dialog()

        def discard(e):
            self.page.pop_dialog()
            self._do_remove_loaded_script()

        async def save_and_remove(e):
            self.page.pop_dialog()
            await self.save_llp_script(None)
            if not self.has_unsaved_changes:  # save genuinely succeeded
                self._do_remove_loaded_script()

        return ft.AlertDialog(
            modal=True,
            title=ft.Text("Unsaved changes"),
            content=ft.Text("This project has changes that haven't been saved. Save before removing it?"),
            actions=[
                ft.Button("Cancel", on_click=cancel),
                ft.Button("Discard & Remove", on_click=discard),
                ft.Button("Save & Remove", on_click=save_and_remove),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

    # -----------------------------------------------------------------
    # Stub functions
    # -----------------------------------------------------------------

    async def _download_missing_voices(self, voice_ids: list):
        """Downloads a list of voice models the device doesn't have yet"""
        self._downloading_voices = voice_ids
        self._downloading_voices_current = None
        self.build_menu()

        loop = asyncio.get_event_loop()
        failed = []
        for voice_id in voice_ids:
            self._downloading_voices_current = voice_id
            self.build_menu()
            try:
                await loop.run_in_executor(None, download_voice_and_ensure_audition_sync, voice_id)
            except Exception:
                failed.append(voice_id)

        self._downloading_voices = None
        self._downloading_voices_current = None
        self.build_menu()

        if failed:
            failed_labels = ", ".join(voice_label_for(v) for v in failed)
            self.page.show_dialog(
                ft.SnackBar(ft.Text(f"Couldn't download: {failed_labels}. Try again once you're back online."))
            )

    async def _prepare_voices_for_scope(self, characters: list):
        """Loads into memory whichever voice each character in `characters` needs"""
        self._preparing_voices = characters
        self._preparing_voices_current = None
        self.build_menu()

        loop = asyncio.get_event_loop()
        failed = []
        for character in characters:
            self._preparing_voices_current = character
            self.build_menu()
            voice_id = self._resolve_voice_for_character(character)
            try:
                await loop.run_in_executor(None, _get_loaded_voice, voice_id)
            except Exception:
                failed.append(character)

        self._preparing_voices = None
        self._preparing_voices_current = None
        self.build_menu()

        if failed:
            failed_list = ", ".join(failed)
            self.page.show_dialog(
                ft.SnackBar(ft.Text(f"Couldn't prepare voices for: {failed_list} - will retry when needed."))
            )

    async def _apply_loaded_llp_bundle(self, bundle: dict, filename: str, full_path: "Path | None", verb: str = "Loaded"):
        """Everything that happens after an .llp has been successfully parsed (see parse_llp_bundle)"""
        self.loaded_script_filename = filename
        self.loaded_script_path = full_path
        self.raw_script_text = None  # no raw text for an already-processed .llp - AnalyseScript doesn't apply here
        self.parsed_script = bundle["script"]
        self.roles = bundle["roles"]
        self.has_unsaved_changes = False  # a fresh load matches what's on disk - nothing to save yet

        self.build_menu()  # refresh "Script Loaded:" immediately, same reasoning as LoadtxtScript

        missing_voice_ids = sorted({
            role.voice for role in self.roles
            if role.voice and not is_voice_downloaded(role.voice)
        })

        if missing_voice_ids:
            await self._download_missing_voices(missing_voice_ids)

        self.page.show_dialog(
            ft.SnackBar(ft.Text(f"{verb} '{filename}' - {len(self.roles)} role(s) ready."))
        )

    async def load_llp_script(self, e):
        """Same cross-platform file-picking approach as LoadtxtScript"""
        filename, file_bytes, full_path = await self._pick_file_bytes(
            dialog_title="Select a LineLearnerPro (.llp) file",
            tkinter_filetypes=[("LineLearnerPro files", "*.llp"), ("All files", "*.*")],
            flet_file_type=ft.FilePickerFileType.CUSTOM,
            flet_allowed_extensions=["llp"],
        )

        if filename is None:
            return  # cancelled, or _pick_file_bytes already showed an error

        try:
            bundle = parse_llp_bundle(file_bytes)
        except ValueError as ex:
            self.page.show_dialog(ft.SnackBar(ft.Text(str(ex))))
            return

        await self._apply_loaded_llp_bundle(bundle, filename, full_path)

    def read_script_aloud(self, e):
        """Checks a script is actually loaded/analysed, then navigates to the scope-selection screen"""
        if not self.parsed_script:
            self.page.show_dialog(
                ft.SnackBar(ft.Text("Load or analyse a script first - there's nothing to read yet."))
            )
            return
        self._read_aloud_mode = "read_aloud"
        self.menu_mode = 5
        self.build_menu()

    def _scope_has_selected_role(self, option: dict) -> bool:
        """True if at least one of the actor's own selected role(s) has a line within this scope option"""
        blocks = get_blocks_for_scope(self.parsed_script, option["act_index"], option["scene_index"])
        characters = get_distinct_characters_for_scope(blocks)
        return bool(self.my_selected_roles.intersection(characters))

    def build_read_aloud_scope_menu(self) -> ft.Column:
        """The scope-selection screen itself"""
        options = build_read_aloud_scope_options(self.parsed_script)

        titles = {
            "read_aloud": "Read Script Aloud",
            "read_along": "Script Read-along",
            "rehearse": "Rehearse My Lines",
        }
        rows = [ft.Text(titles.get(self._read_aloud_mode, "Read Script Aloud"), size=20, weight=ft.FontWeight.W_500)]
        for option in options:
            # Only Read-along/Rehearse depend on the actor's own lines actually falling within the chosen scope
            disabled = (
                self._read_aloud_mode in ("read_along", "rehearse")
                and not self._scope_has_selected_role(option)
            )
            rows.append(self.menu_button(
                option["label"],
                self._make_read_aloud_scope_handler(option),
                icon=ft.Icons.RECORD_VOICE_OVER,
                disabled=disabled,
            ))
        rows.append(self.menu_button("Return to main menu", self.return_to_main_menu, icon=ft.Icons.ARROW_BACK))

        return ft.Column(rows, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _read_aloud_header_text(self) -> str:
        """"Reading: ..." text for the live reading screen's header"""
        parts = [p for p in (self._read_aloud_current_act_label, self._read_aloud_current_scene_label) if p]
        return f"Reading: {' '.join(parts)}" if parts else "Reading..."

    def _build_action_lozenge(self, label: str, on_click, bgcolor, color, disabled: bool = False) -> ft.Container:
        """One rounded, pill-shaped action button for the reading screen's action bar. disabled shows it greyed-out and unclickable - used by
        Smart Cue, whose availability changes as reading progresses (see _smart_cue_enabled/_update_smart_cue_button_state)."""
        return ft.Container(
            content=ft.Text(label, size=16, weight=ft.FontWeight.W_500, color=ft.Colors.ON_SURFACE_VARIANT if disabled else color),
            on_click=None if disabled else on_click,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST if disabled else bgcolor,
            padding=ft.padding.Padding.symmetric(horizontal=24, vertical=10),
            border_radius=24,
            opacity=0.5 if disabled else 1.0,
        )

    def _smart_cue_target_index(self):
        """Where Smart Cue would currently jump to, or None if it's not available right now - either no own line is left ahead in this scope,
        or it's already closer than the target lead-in."""
        return compute_smart_cue_target_index(self._read_aloud_entries, self._read_aloud_block_index, self.my_selected_roles)

    def _smart_cue_enabled(self) -> bool:
        """Whether the Smart Cue lozenge should currently be clickable - not while genuinely paused waiting on the user's own line"""
        if not self._read_aloud_active or self._read_along_waiting_for_continue:
            return False
        return self._smart_cue_target_index() is not None

    def _set_smart_cue_button_look(self, enabled: bool):
        """Applies the enabled/disabled look to the Smart Cue lozenge in place, without recomputing eligibility itself"""
        if not self._smart_cue_button:
            return
        self._smart_cue_button.content.color = ft.Colors.ON_SECONDARY if enabled else ft.Colors.ON_SURFACE_VARIANT
        self._smart_cue_button.bgcolor = ft.Colors.SECONDARY if enabled else ft.Colors.SURFACE_CONTAINER_HIGHEST
        self._smart_cue_button.on_click = self.on_smart_cue_pressed if enabled else None
        self._smart_cue_button.opacity = 1.0 if enabled else 0.5
        self._smart_cue_button.update()

    def _update_smart_cue_button_state(self):
        """Refreshes the Smart Cue lozenge's enabled/disabled look in place by recomputing eligibility from scratch"""
        self._set_smart_cue_button_look(self._smart_cue_enabled())

    def on_smart_cue_pressed(self, e):
        """Smart Cue lozenge handler - jumps reading ahead to about SMART_CUE_TARGET_SECONDS before the user's next own line"""
        if not self._smart_cue_enabled():
            return
        target_index = self._smart_cue_target_index()
        if target_index is None:
            return
        self._read_aloud_skip_to_index = target_index
        # Greyed out immediately, rather than waiting for the next normal recomputation
        self._set_smart_cue_button_look(False)

    async def _fast_forward_through_skip(self, entries: list, from_index: int, to_index: int):
        """Smart Cue's visual pass-through for the blocks being skipped (from_index up to but not including to_index) - each one is briefly shown"""
        for i in range(from_index, to_index):
            if self._read_aloud_stop_requested:
                return
            block = entries[i][0]
            if block["type"] == "direction":
                self._append_read_aloud_transcript_entry({
                    "type": "direction",
                    "character": None,
                    "text": block["text"],
                    "photo": None,
                    "skipped": True,
                })
            elif block["type"] == "line":
                self._append_read_aloud_transcript_entry({
                    "type": "line",
                    "character": block["character"],
                    "text": block["text"],
                    "spoken_text": block["spoken_text"],
                    "photo": self._photo_for_character(block["character"]),
                    "listening": False,
                    "skipped": True,
                })
            # "unrecognised" blocks are never shown even during normal reading - skip them here too
            await asyncio.sleep(SMART_CUE_SKIP_STEP_SECONDS)

    def _build_read_aloud_action_bar(self) -> ft.Row:
        """The row of controls directly beneath the reading screen's header - Quit always, 
        plus a primary-styled lozenge in read-along mode (Continue) and rehearse mode (Done),
        plus Smart Cue in all three modes (Read Script Aloud, Script Read-along, Rehearse My Lines)"""
        buttons = []
        if self._read_aloud_mode == "read_along":
            buttons.append(
                self._build_action_lozenge("Continue", self.continue_read_along, ft.Colors.PRIMARY, ft.Colors.ON_PRIMARY)
            )
        elif self._read_aloud_mode == "rehearse":
            buttons.append(
                self._build_action_lozenge("Done", self.continue_read_along, ft.Colors.PRIMARY, ft.Colors.ON_PRIMARY)
            )
        self._smart_cue_button = self._build_action_lozenge(
            "Smart Cue", self.on_smart_cue_pressed, ft.Colors.SECONDARY, ft.Colors.ON_SECONDARY,
            disabled=not self._smart_cue_enabled(),
        )
        buttons.append(self._smart_cue_button)
        buttons.append(self._build_action_lozenge("Quit", self.quit_read_aloud, ft.Colors.ERROR, ft.Colors.ON_ERROR))
        return ft.Row(buttons, alignment=ft.MainAxisAlignment.CENTER, spacing=16)

    def _build_transcript_entry_control(self, entry: dict) -> ft.Control:
        """One entry in the reading screen's transcript. A "skipped": True entry (from Smart Cue's fast-forward pass - see _fast_forward_through_skip)
        renders dimmed, so it's visibly clear it was skipped past rather than performed."""
        opacity = 0.45 if entry.get("skipped") else 1.0

        if entry["type"] == "direction":
            return ft.Container(
                content=ft.Text(
                    entry["text"],
                    size=14,
                    italic=True,
                    color=STAGE_DIRECTION_COLOR,
                    text_align=ft.TextAlign.CENTER,
                ),
                padding=ft.padding.Padding.symmetric(horizontal=16, vertical=8),
                alignment=ft.alignment.Alignment.CENTER,
                opacity=opacity,
            )

        if entry.get("listening"):
            text_control = ft.Text("Listening...", size=24, italic=True, weight=ft.FontWeight.W_500)
        else:
            # Rehearse My Lines' scoring flag
            flag_color = "#FF0000" if entry.get("is_flagged") else None
            text_control = ft.Text(spans=build_transcript_line_spans(entry["text"], override_color=flag_color), size=16)

        column_children = [
            ft.Text(entry["character"], size=14, weight=ft.FontWeight.W_500),
            text_control,
        ]
        # Stage 1 of Rehearse My Lines' speech-to-text: just show what Vosk actually recognized during the pause
        heard_text = entry.get("heard_text", "").strip()
        if heard_text:
            column_children.append(
                ft.Text(
                    f'Heard: "{heard_text}"',
                    size=13,
                    italic=True,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                )
            )

        return ft.Container(
            content=ft.Row(
                [
                    ft.Image(src=entry["photo"], width=96, height=96, fit=ft.BoxFit.CONTAIN),
                    ft.Column(
                        column_children,
                        spacing=2,
                        expand=True,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            padding=ft.padding.Padding.symmetric(horizontal=16, vertical=4),
            opacity=opacity,
        )

    def build_read_aloud_screen(self) -> ft.Column:
        """The full-screen live reading view"""
        self._read_aloud_reading_text = ft.Text(
            self._read_aloud_header_text(),
            size=18,
            weight=ft.FontWeight.W_500,
            text_align=ft.TextAlign.CENTER,
        )

        header = ft.Container(
            content=ft.Column(
                [
                    ft.Image(src=self.logo_image, width=128, height=128, fit=ft.BoxFit.CONTAIN),
                    ft.Text(
                        f"Script Loaded: {self.loaded_script_filename or 'none'}",
                        size=12,
                        italic=True,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    self._read_aloud_reading_text,
                    self._build_read_aloud_action_bar(),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding.symmetric(horizontal=16, vertical=12),
            # Deliberately NOT expand=1
            shadow=ft.BoxShadow(
                offset=ft.Offset(0, 4),
                blur_radius=8,
                spread_radius=0,
                color=ft.Colors.with_opacity(0.4, ft.Colors.BLACK),
            ),
        )

        self._read_aloud_listview = ft.ListView(
            controls=[self._build_transcript_entry_control(entry) for entry in self._read_aloud_transcript],
            auto_scroll=True,
            auto_scroll_animation=ft.Animation(duration=250, curve=ft.AnimationCurve.EASE_OUT),
            scroll=ft.ScrollMode.ADAPTIVE,  # visible scrollbar on desktop/web, same reasoning as page.scroll above
            spacing=4,
            padding=ft.padding.Padding.symmetric(horizontal=8, vertical=8),
            expand=True,
        )

        # Wrapped in a GestureDetector so the mouse cursor can be shown/hidden dynamically
        self._read_aloud_cursor_region = ft.GestureDetector(
            content=ft.Column([header, self._read_aloud_listview], spacing=0, expand=True),
            mouse_cursor=ft.MouseCursor.NONE,
            hover_interval=200,
            on_hover=self._on_read_aloud_mouse_move,
            expand=True,
        )
        return self._read_aloud_cursor_region

    def _cancel_cursor_hide_task(self):
        """Cancels any pending "hide the cursor again" timer"""
        if self._cursor_hide_task and not self._cursor_hide_task.done():
            self._cursor_hide_task.cancel()
        self._cursor_hide_task = None

    def _on_read_aloud_mouse_move(self, e):
        """Shows the cursor immediately on movement"""
        self._cancel_cursor_hide_task()

        if self._read_aloud_cursor_region.mouse_cursor != ft.MouseCursor.BASIC:
            self._read_aloud_cursor_region.mouse_cursor = ft.MouseCursor.BASIC
            self._read_aloud_cursor_region.update()

        self._cursor_hide_task = asyncio.create_task(self._hide_cursor_after_delay())

    async def _hide_cursor_after_delay(self):
        try:
            await asyncio.sleep(CURSOR_HIDE_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        if self._read_aloud_active and self._read_aloud_cursor_region:
            self._read_aloud_cursor_region.mouse_cursor = ft.MouseCursor.NONE
            self._read_aloud_cursor_region.update()

    def _append_read_aloud_transcript_entry(self, entry: dict):
        """Adds ONE new entry to the live transcript"""
        self._read_aloud_transcript.append(entry)
        self._read_aloud_listview.controls.append(self._build_transcript_entry_control(entry))
        self._read_aloud_listview.update()

    def _reveal_read_aloud_line(self, index: int, heard_text: str = ""):
        """Replaces a "Listening..." entry with the normal line-text rendering"""
        entry = self._read_aloud_transcript[index]
        entry["listening"] = False
        entry["heard_text"] = heard_text
        entry["match_score"] = _score_heard_text(entry.get("spoken_text", entry["text"]), heard_text)
        entry["is_flagged"] = entry["match_score"] < LINE_MATCH_THRESHOLD
        self._read_aloud_listview.controls[index] = self._build_transcript_entry_control(entry)
        self._read_aloud_listview.update()

    def _update_read_aloud_header_text(self):
        """Refreshes just the "Reading: ..." text in place, same reasoning as _append_read_aloud_transcript_entry"""
        self._read_aloud_reading_text.value = self._read_aloud_header_text()
        self._read_aloud_reading_text.update()

    def _resolve_voice_for_character(self, character: str) -> str:
        """Which voice ID to actually use for a character's lines"""
        role = next((r for r in self.roles if r.character == character), None)
        if role and role.voice and is_voice_downloaded(role.voice):
            return role.voice
        return FALLBACK_VOICE_ID

    def _photo_for_character(self, character: str):
        """The photo (or fallback icon/logo) to show in the transcript for a given character - shared
        by the main reading loop and Smart Cue's skip-through pass (_fast_forward_through_skip)."""
        role = next((r for r in self.roles if r.character == character), None)
        if role and role.photo:
            return role.photo
        if role:
            return self._default_icon_source(role)
        return self.logo_image

    async def _play_one_unit(self, voice_id: str, unit_text: str):
        """Synthesizes and plays ONE small unit of text (a sentence, or one clause-piece of an unusually long sentence)"""
        loop = asyncio.get_event_loop()
        wav_bytes = await loop.run_in_executor(None, synthesize_wav_bytes_sync, voice_id, unit_text)

        if self.is_desktop:
            await loop.run_in_executor(None, play_wav_bytes_sync, wav_bytes)
            return

        from flet_audio.types import AudioState

        finished = asyncio.Event()

        def on_state_change(ev):
            if ev.state == AudioState.COMPLETED:
                finished.set()

        previous_handler = self.voice_player.on_state_change
        self.voice_player.on_state_change = on_state_change
        self.voice_player.src = wav_bytes
        await self.voice_player.play()
        await finished.wait()
        self.voice_player.on_state_change = previous_handler

    async def _play_line_audio(self, character: str, spoken_text: str) -> bool:
        """Plays one whole line, unit by unit. Returns False if playback was stopped early (Quit was
        pressed mid-line, or Smart Cue interrupted it to jump elsewhere)."""
        voice_id = self._resolve_voice_for_character(character)
        groups = _split_text_for_synthesis(spoken_text)

        for group_index, group in enumerate(groups):
            for unit in group:
                if self._read_aloud_stop_requested or self._read_aloud_skip_to_index is not None:
                    return False
                await self._play_one_unit(voice_id, unit)

            if self._read_aloud_stop_requested or self._read_aloud_skip_to_index is not None:
                return False
            if group_index < len(groups) - 1:
                await asyncio.sleep(SENTENCE_PAUSE_SECONDS)

        return True

    async def _read_lines_for_scope(self, option: dict):
        """The audio+transcript side of ReadScriptAloud/ScriptReadAlong - see self._read_aloud_mode for which one's currently active."""
        entries = get_blocks_with_scope_context(self.parsed_script, option["act_index"], option["scene_index"])
        self._read_aloud_entries = entries
        blocks = [entry[0] for entry in entries]

        # Warm up every voice this scope will actually need BEFORE reading starts
        characters_needed = get_distinct_characters_for_scope(blocks)
        if self._read_aloud_mode in ("read_along", "rehearse"):
            characters_needed = [c for c in characters_needed if c not in self.my_selected_roles]
        if characters_needed:
            await self._prepare_voices_for_scope(characters_needed)

        # An index-based loop rather than a plain "for" - Smart Cue needs to be able to redirect
        # where reading continues from (self._read_aloud_block_index), not just step through in order.
        self._read_aloud_block_index = 0
        while self._read_aloud_block_index < len(entries):
            if self._read_aloud_stop_requested:
                return

            block, act_label, scene_label = entries[self._read_aloud_block_index]

            self._read_aloud_current_act_label = act_label or ""
            self._read_aloud_current_scene_label = scene_label or ""
            self._update_read_aloud_header_text()
            self._update_smart_cue_button_state()

            if block["type"] == "direction":
                self._append_read_aloud_transcript_entry({
                    "type": "direction",
                    "character": None,
                    "text": block["text"],
                    "photo": None,
                })
                self._read_aloud_block_index += 1
                continue

            if block["type"] != "line":
                self._read_aloud_block_index += 1
                continue  # "unrecognised" blocks aren't shown or spoken

            photo = self._photo_for_character(block["character"])

            is_users_line = block["character"] in self.my_selected_roles

            # In read-along mode the line is always shown exactly as usual, regardless of whose it is
            is_listening_entry = self._read_aloud_mode == "rehearse" and is_users_line
            self._append_read_aloud_transcript_entry({
                "type": "line",
                "character": block["character"],
                "text": block["text"],
                "spoken_text": block["spoken_text"],  # for scoring against heard_text - see _reveal_read_aloud_line
                "photo": photo,
                "listening": is_listening_entry,
            })

            if self._read_aloud_mode in ("read_along", "rehearse") and is_users_line:
                # This is the user's OWN line - they perform it themselves (reading along with the script, or from memory in rehearse mode).
                # Smart Cue is disabled while this wait is happening (see _smart_cue_enabled) - there's nothing sensible for it to do here
                listening_entry_index = len(self._read_aloud_transcript) - 1 if is_listening_entry else None
                self._read_along_waiting_for_continue = True
                self._update_smart_cue_button_state()
                self._read_along_continue_event.clear()
                await self._start_listening()
                await self._read_along_continue_event.wait()
                heard_text = await self._stop_listening()
                self._read_along_waiting_for_continue = False
                if self._read_aloud_stop_requested:
                    # Quit is what unblocked the wait
                    return
                if listening_entry_index is not None:
                    # Done was genuinely pressed - reveal the actual line now that the user has said it
                    self._reveal_read_aloud_line(listening_entry_index, heard_text)
                self._read_aloud_block_index += 1
                continue

            spoken_text = block["spoken_text"]
            if not spoken_text:
                self._read_aloud_block_index += 1
                continue  # nothing to actually say (e.g. a line that was entirely a stage direction)

            try:
                completed = await self._play_line_audio(block["character"], spoken_text)
            except Exception as ex:
                # Covers a synthesis/playback failure that survived even _get_loaded_voice's own self-healing retry
                self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't read this line: {ex}")))
                return

            if self._read_aloud_stop_requested:
                return
            if not completed and self._read_aloud_skip_to_index is not None:
                # Interrupted by Smart Cue specifically, not Quit - fast-forward visibly through what's being skipped
                skip_target = self._read_aloud_skip_to_index
                self._read_aloud_skip_to_index = None
                await self._fast_forward_through_skip(entries, self._read_aloud_block_index + 1, skip_target)
                if self._read_aloud_stop_requested:
                    return
                self._read_aloud_block_index = skip_target
                continue
            if not completed:
                return  # stopped for some other reason
            self._read_aloud_block_index += 1

        # The scope has finished naturally (every block processed, not stopped early or errored out) - pause here
        self._read_aloud_waiting_at_end = True
        self._update_smart_cue_button_state()  # nothing left to cue toward once the scope has ended
        self._read_along_continue_event.clear()
        await self._read_along_continue_event.wait()
        self._read_aloud_waiting_at_end = False

    def _make_read_aloud_scope_handler(self, option: dict):
        """Factory, same late-binding-closure reason as elsewhere."""
        async def handler(e):
            self._read_aloud_scope = option
            self._read_aloud_transcript = []
            self._read_aloud_stop_requested = False
            self._read_aloud_current_act_label = ""
            self._read_aloud_current_scene_label = ""
            self._read_along_waiting_for_continue = False
            self._read_along_continue_event = asyncio.Event()
            self._read_aloud_active = True
            self._read_aloud_entries = []
            self._read_aloud_block_index = 0
            self._read_aloud_skip_to_index = None
            # ScrollMode.HIDDEN does NOT disable scrolling
            self.page.scroll = None
            self.build_menu()

            await self._read_lines_for_scope(option)

            self._read_aloud_active = False
            self.page.scroll = ft.ScrollMode.ADAPTIVE
            self._cancel_cursor_hide_task()
            self.menu_mode = 5
            self.build_menu()
        return handler

    def quit_read_aloud(self, e):
        """The Quit lozenge on the reading screen - stops the read-through as close to immediately as possible."""
        self._read_aloud_stop_requested = True
        self._read_aloud_active = False
        self.page.scroll = ft.ScrollMode.ADAPTIVE
        self._cancel_cursor_hide_task()
        # If the background reading task is currently PAUSED waiting for Continue, it would otherwise stay blocked on that event forever
        if self._read_along_continue_event:
            self._read_along_continue_event.set()
        self.menu_mode = 5
        self.build_menu()

    def continue_read_along(self, e):
        """The Continue/Done lozenge"""
        if self._read_along_continue_event and (
            self._read_along_waiting_for_continue or self._read_aloud_waiting_at_end
        ):
            self._read_along_continue_event.set()

    async def _start_listening(self):
        """Starts capturing the microphone (via sounddevice, directly - see setup_speech_recorder) and feeding it to a fresh Vosk recognizer"""
        self._last_partial_transcript = ""
        self._heard_any_speech = False
        self._recognized_segments = []  # reset BEFORE the early-return below
        tier_entry = _hearing_quality_tier_by_name(self.hearing_quality_tier)
        if tier_entry is None:
            return  # shouldn't happen - self.hearing_quality_tier is always set by load_user_preferences
        model_name = tier_entry["model_name"]
        if not is_vosk_model_downloaded(model_name):
            return
        self._speech_recognizer = vosk.KaldiRecognizer(_get_vosk_model(model_name), 16000)
        self._speech_recognizer.SetWords(True)  # per-word start/end/conf in Result()

        loop = asyncio.get_event_loop()

        def audio_callback(indata, frames, time_info, status):
            # Runs on sounddevice's OWN audio thread, NOT the asyncio event loop - must hand the chunk back to the main thread safely
            loop.call_soon_threadsafe(self._handle_speech_chunk, bytes(indata))

        try:
            self._speech_stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                callback=audio_callback,
            )
            self._speech_stream.start()
        except Exception:
            # Covers no microphone available/permission denied - same reasoning as a missing Vosk model: auto-advance just isn't available
            self._speech_recognizer = None
            self._speech_stream = None

    async def _stop_listening(self) -> str:
        """Stops microphone capture and cleans up - called once the current pause on the user's own line ends"""
        if self._silence_timeout_task and not self._silence_timeout_task.done():
            self._silence_timeout_task.cancel()
        self._silence_timeout_task = None

        if self._speech_recognizer is not None:
            raw_final = json.loads(self._speech_recognizer.FinalResult())
            final_segment = _filter_spurious_long_words(raw_final)
            if final_segment:
                self._recognized_segments.append(final_segment)
        self._speech_recognizer = None

        if self._speech_stream is not None:
            try:
                self._speech_stream.stop()
                self._speech_stream.close()
            except Exception:
                pass  # not fatal if it was never actually running
            self._speech_stream = None

        return " ".join(self._recognized_segments)

    def _handle_speech_chunk(self, chunk: bytes):
        """Called (via call_soon_threadsafe from the sounddevice audio thread - see _start_listening) on the main event loop"""
        if self._speech_recognizer is None:
            return
        if self._speech_recognizer.AcceptWaveform(chunk):
            raw_result = json.loads(self._speech_recognizer.Result())
            segment_text = _filter_spurious_long_words(raw_result)
            if segment_text:
                self._recognized_segments.append(segment_text)

        partial = json.loads(self._speech_recognizer.PartialResult()).get("partial", "")

        if partial and partial != self._last_partial_transcript:
            self._last_partial_transcript = partial
            self._heard_any_speech = True
            if self._silence_timeout_task and not self._silence_timeout_task.done():
                self._silence_timeout_task.cancel()
            self._silence_timeout_task = asyncio.create_task(self._auto_advance_after_silence())

    async def _auto_advance_after_silence(self):
        try:
            await asyncio.sleep(SILENCE_THRESHOLD_SECONDS)
        except asyncio.CancelledError:
            return
        if self._heard_any_speech:
            self.continue_read_along(None)

    def _handle_keyboard_event(self, e: ft.KeyboardEvent):
        """Two keyboard shortcuts on the reading screen"""
        if self._read_aloud_active:
            if e.key == "Escape":
                self.quit_read_aloud(e)
            elif self._read_aloud_waiting_at_end or self._read_aloud_mode in ("read_along", "rehearse"):
                self.continue_read_along(e)
            return

        if e.key == "Escape":
            handler_name = self.MENU_MODE_BACK_HANDLER_NAMES.get(self.menu_mode)
            if handler_name is not None:
                getattr(self, handler_name)(e)

    def select_my_roles(self, e):
        """Navigates to the role-selection screen - lets the user mark which role(s) THEY are responsible for."""
        if not self.roles:
            self.page.show_dialog(
                ft.SnackBar(ft.Text("Load or analyse a script first - there are no roles to select yet."))
            )
            return
        self.menu_mode = 6
        self.build_menu()

    def build_select_my_roles_menu(self) -> ft.Column:
        """The role-selection screen itself. Multi-select, not exclusive - an actor can be responsible for more than one role."""
        rows = [ft.Text("Select My Role(s)", size=20, weight=ft.FontWeight.W_500)]
        for role in self.roles:
            rows.append(self._build_role_selection_row(role))
        rows.append(self.menu_button("Return to main menu", self.return_to_main_menu, icon=ft.Icons.ARROW_BACK))
        return ft.Column(rows, spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _build_role_selection_row(self, role: "Role") -> ft.Container:
        """One row per role: photo/default icon, character name, and a Select/Selected toggle"""
        if role.photo:
            image = ft.Image(src=role.photo, width=96, height=96, fit=ft.BoxFit.CONTAIN)
        else:
            image = ft.Image(src=self._default_icon_source(role), width=96, height=96, fit=ft.BoxFit.CONTAIN)

        selected = role.character in self.my_selected_roles

        return ft.Container(
            content=ft.Row(
                [
                    image,
                    ft.Text(role.character, size=16, expand=True),
                    ft.Button(
                        "Selected" if selected else "Select",
                        on_click=self._make_toggle_my_role_handler(role.character),
                        height=self.BUTTON_HEIGHT,
                        bgcolor=ft.Colors.PRIMARY if selected else None,
                        color=ft.Colors.ON_PRIMARY if selected else None,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=self._role_row_width(),
            padding=ft.padding.Padding.symmetric(horizontal=8, vertical=4),
        )

    def _make_toggle_my_role_handler(self, character: str):
        """Factory, same late-binding-closure reason as elsewhere."""
        def handler(e):
            if character in self.my_selected_roles:
                self.my_selected_roles.discard(character)
            else:
                self.my_selected_roles.add(character)
            self.build_menu()
        return handler

    def open_pronunciation_dictionary(self, e):
        """Navigates to the pronunciation dictionary list"""
        self.menu_mode = 7
        self.build_menu()

    def build_pronunciation_dictionary_menu(self) -> ft.Column:
        rows = [ft.Text("Pronunciation Dictionary", size=20, weight=ft.FontWeight.W_500)]
        if not self.pronunciation_entries:
            rows.append(ft.Text("No entries yet.", size=14, text_align=ft.TextAlign.CENTER))
        else:
            for entry in self.pronunciation_entries:
                rows.append(self._build_pronunciation_entry_row(entry))
        # "Add new word" and "Return to settings" are ordinary navigation buttons, not word-list rows
        rows.append(
            ft.Column(
                [
                    self.menu_button("Add new word", self.add_pronunciation_entry, icon=ft.Icons.ADD),
                    self.menu_button("Return to settings", self.return_to_settings, icon=ft.Icons.ARROW_BACK),
                ],
                spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
        return ft.Column(rows, spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _build_pronunciation_entry_row(self, entry: dict) -> ft.Container:
        """One row per dictionary entry: the original word, its phonetic respelling underneath, and Edit/Delete actions"""
        return ft.Container(
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(entry["word"], size=16, weight=ft.FontWeight.W_500),
                            ft.Text(entry["phonetic"], size=13, italic=True),
                        ],
                        spacing=0,
                        expand=True,
                    ),
                    ft.Button(
                        "Edit",
                        on_click=self._make_edit_pronunciation_handler(entry["word"]),
                        height=self.BUTTON_HEIGHT,
                    ),
                    ft.Button(
                        "Delete",
                        on_click=self._make_delete_pronunciation_handler(entry["word"]),
                        height=self.BUTTON_HEIGHT,
                        bgcolor=ft.Colors.ERROR,
                        color=ft.Colors.ON_ERROR,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=self._role_row_width(),
            padding=8,
        )

    def add_pronunciation_entry(self, e):
        self._pronunciation_editing_word = None
        self.menu_mode = 8
        self.build_menu()

    def _make_edit_pronunciation_handler(self, word: str):
        """Factory, same late-binding-closure reason as elsewhere."""
        def handler(e):
            self._pronunciation_editing_word = word
            self.menu_mode = 8
            self.build_menu()
        return handler

    def _make_delete_pronunciation_handler(self, word: str):
        """Factory, same late-binding-closure reason as elsewhere."""
        async def handler(e):
            self.pronunciation_entries = [
                entry for entry in self.pronunciation_entries if entry["word"].lower() != word.lower()
            ]
            save_pronunciation_dictionary_sync(self.pronunciation_entries)
            self.build_menu()
            await self._reapply_pronunciation_dictionary_to_current_project()
        return handler

    def build_pronunciation_edit_menu(self) -> ft.Column:
        """The add/edit screen for a single dictionary entry"""
        editing_entry = None
        if self._pronunciation_editing_word is not None:
            editing_entry = next(
                (e for e in self.pronunciation_entries
                 if e["word"].lower() == self._pronunciation_editing_word.lower()),
                None,
            )

        self._pronunciation_word_field = ft.TextField(
            label="Word",
            value=editing_entry["word"] if editing_entry else "",
            width=self._role_row_width(),
        )
        self._pronunciation_phonetic_field = ft.TextField(
            label="Phonetic spelling",
            value=editing_entry["phonetic"] if editing_entry else "",
            width=self._role_row_width(),
        )

        title = "Edit Word" if editing_entry else "Add Word"

        rows = [
            ft.Text(title, size=20, weight=ft.FontWeight.W_500),
            self._pronunciation_word_field,
            self._pronunciation_phonetic_field,
            self.menu_button("Say This", self._make_say_this_pronunciation_handler(), icon=ft.Icons.VOLUME_UP),
            self.menu_button("Save", self._save_pronunciation_entry, icon=ft.Icons.CHECK),
            self.menu_button("Cancel", self._cancel_pronunciation_edit, icon=ft.Icons.CLOSE),
        ]
        return ft.Column(rows, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _make_say_this_pronunciation_handler(self):
        """"Say This" - plays back whatever's CURRENTLY in the phonetic spelling field"""
        async def handler(e):
            phonetic = (self._pronunciation_phonetic_field.value or "").strip()
            if not phonetic:
                self.page.show_dialog(ft.SnackBar(ft.Text("Type a phonetic spelling first.")))
                return
            try:
                await self._play_one_unit(FALLBACK_VOICE_ID, phonetic)
            except Exception as ex:
                self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't play that: {ex}")))
        return handler

    async def _save_pronunciation_entry(self, e):
        word = (self._pronunciation_word_field.value or "").strip()
        phonetic = (self._pronunciation_phonetic_field.value or "").strip()

        if not word or not phonetic:
            self.page.show_dialog(ft.SnackBar(ft.Text("Both a word and a phonetic spelling are needed.")))
            return

        # Remove any existing entry for either the word being edited
        entries = [
            entry for entry in self.pronunciation_entries
            if entry["word"].lower() != word.lower()
            and (self._pronunciation_editing_word is None
                 or entry["word"].lower() != self._pronunciation_editing_word.lower())
        ]
        entries.append({"word": word, "phonetic": phonetic})
        save_pronunciation_dictionary_sync(entries)
        self.pronunciation_entries = load_pronunciation_dictionary_sync()

        self._pronunciation_editing_word = None
        self.menu_mode = 7
        self.build_menu()
        await self._reapply_pronunciation_dictionary_to_current_project()

    def _cancel_pronunciation_edit(self, e):
        self._pronunciation_editing_word = None
        self.menu_mode = 7
        self.build_menu()

    async def _reapply_pronunciation_dictionary_to_current_project(self):
        """Re-runs the pronunciation dictionary against whatever's currently loaded, so an edit takes effect immediately"""
        if self.raw_script_text:
            self.analyse_script(None)
            return

        if not self.parsed_script:
            return

        reapply_pronunciation_dictionary_to_parsed_script(self.parsed_script, self.pronunciation_entries)
        self._mark_dirty()
        await self.save_llp_script(None)

    def script_read_along(self, e):
        """Same scope-selection screen as Read Script Aloud"""
        if not self.parsed_script:
            self.page.show_dialog(
                ft.SnackBar(ft.Text("Load or analyse a script first - there's nothing to read yet."))
            )
            return
        self._read_aloud_mode = "read_along"
        self.menu_mode = 5
        self.build_menu()

    def rehearse_my_lines(self, e):
        """Same scope-selection screen as Read Script Aloud"""
        if not self.parsed_script:
            self.page.show_dialog(
                ft.SnackBar(ft.Text("Load or analyse a script first - there's nothing to rehearse yet."))
            )
            return
        self._read_aloud_mode = "rehearse"
        self.menu_mode = 5
        self.build_menu()

    def _tkinter_pick_file_sync(self, title: str, filetypes: list) -> str:
        """Blocking helper - runs OFF the asyncio event loop, since opening a file dialog is synchronous and would otherwise freeze
        the whole app while open. On Windows, uses the native Win32 dialog via ctypes. On Linux, uses zenity (see
        _zenity_pick_file_sync) - tkinter is confirmed NOT to reliably bundle with this app's Python packaging on Linux"""
        if self.page.platform == ft.PagePlatform.WINDOWS:
            return self._win32_pick_file_sync(title, filetypes)
        if self.page.platform == ft.PagePlatform.LINUX:
            return self._zenity_pick_file_sync(title, filetypes)

        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()               # we only want the dialog, not an empty root window
        root.attributes("-topmost", True)  # make sure it appears above the Flet window
        path = filedialog.askopenfilename(title=title, filetypes=filetypes)
        root.destroy()
        return path

    def _zenity_pick_file_sync(self, title: str, filetypes: list) -> str:
        """The Linux-native equivalent of _tkinter_pick_file_sync, via zenity (a standard, commonly pre-installed GTK dialog tool"""
        args = ["zenity", "--file-selection", "--title", title]
        if self._last_file_dialog_dir:
            # A trailing slash tells zenity this is a directory to
            # START in, not a specific file to pre-select.
            args += ["--filename", self._last_file_dialog_dir.rstrip("/") + "/"]
        for label, pattern in filetypes:
            args.append("--file-filter")
            args.append(f"{label} | {pattern}")
        try:
            result = subprocess.run(args, capture_output=True, text=True)
        except FileNotFoundError:
            # zenity itself isn't installed - fail gracefully
            return ""
        if result.returncode != 0:
            return ""  # cancelled, or zenity itself errored - either way, nothing to return
        path = result.stdout.strip()
        if path:
            self._last_file_dialog_dir = str(Path(path).parent)
        return path

    def _win32_pick_file_sync(self, title: str, filetypes: list) -> str:
        """The Windows-native equivalent of _tkinter_pick_file_sync"""
        file_buffer = ctypes.create_unicode_buffer("", 32768)  # generous - handles long paths
        # The filter string contains embedded NUL characters by design
        filter_buffer = ctypes.create_unicode_buffer(_win32_file_filter_string(filetypes))

        ofn = _OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
        ofn.hwndOwner = _win32_foreground_window()
        ofn.lpstrFilter = ctypes.cast(filter_buffer, wintypes.LPCWSTR)
        ofn.lpstrFile = ctypes.cast(file_buffer, wintypes.LPWSTR)
        ofn.nMaxFile = len(file_buffer)
        ofn.lpstrTitle = title
        ofn.Flags = _OFN_EXPLORER | _OFN_FILEMUSTEXIST | _OFN_PATHMUSTEXIST | _OFN_NOCHANGEDIR

        if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return file_buffer.value
        return ""

    async def _pick_file_bytes(
        self,
        dialog_title: str,
        tkinter_filetypes: list,
        flet_file_type,
        flet_allowed_extensions: list | None = None,
    ) -> tuple:
        """Shared cross-platform "pick a file, get its bytes back" helper - Returns (filename, bytes, full_path), or (None, None, None)"""
        if self.is_desktop:
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(
                None, self._tkinter_pick_file_sync, dialog_title, tkinter_filetypes
            )

            if not path:
                return None, None, None  # user cancelled the dialog

            try:
                with open(path, "rb") as f:
                    file_bytes = f.read()
            except OSError as ex:
                self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't read file: {ex}")))
                return None, None, None

            return os.path.basename(path), file_bytes, Path(path)

        pick_kwargs = dict(
            dialog_title=dialog_title,
            file_type=flet_file_type,
            allow_multiple=False,
            # Always ask for raw bytes rather than relying on FilePickerFile.path
            with_data=True,
        )
        if flet_allowed_extensions:
            pick_kwargs["allowed_extensions"] = flet_allowed_extensions

        result = await self.file_picker.pick_files(**pick_kwargs)

        if not result:
            return None, None, None  # user closed/cancelled the dialog

        picked = result[0]

        if not picked.bytes:
            self.page.show_dialog(
                ft.SnackBar(ft.Text(f"Couldn't read '{picked.name}' - no data was returned."))
            )
            return None, None, None

        return picked.name, picked.bytes, None

    async def load_txt_script(self, e):
        """Opens a file dialog restricted to .txt files and reads the chosen script into memory ready for AnalyseScript."""
        filename, file_bytes, full_path = await self._pick_file_bytes(
            dialog_title="Select a script (.txt) file",
            tkinter_filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            flet_file_type=ft.FilePickerFileType.CUSTOM,
            flet_allowed_extensions=["txt"],
        )

        if filename is None:
            return  # cancelled, or _pick_file_bytes already showed an error

        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1", errors="replace")

        self.loaded_script_filename = filename
        self.loaded_script_path = full_path  # None on web/mobile - see SaveLineLearnerProScript
        self.raw_script_text = text
        line_count = len(text.splitlines())

        self.build_menu()  # refresh now, so "Script Loaded:" updates immediately

        self.page.show_dialog(
            ft.SnackBar(ft.Text(f"Loaded '{filename}' - {line_count} lines. Ready to analyse."))
        )

    def analyse_script(self, e):
        """Runs the parser against whatever LoadtxtScript most recently loaded."""
        if not self.raw_script_text:
            self.page.show_dialog(
                ft.SnackBar(ft.Text("Load a script first (Load txt script) before analysing it."))
            )
            return

        self.parsed_script = parse_script_text(self.raw_script_text)
        apply_pronunciation_dictionary_to_script(self.parsed_script, self.pronunciation_entries)

        # Keep any roles that already have assignments (actor/voice/photo)
        existing_by_character = {role.character: role for role in self.roles}
        self.roles = [
            existing_by_character.get(character, Role(character=character))
            for character in self.parsed_script["characters"]
        ]

        act_count = len(self.parsed_script["acts"])
        scene_count = sum(len(act["scenes"]) for act in self.parsed_script["acts"])
        character_count = len(self.roles)

        self._mark_dirty()

        self.page.show_dialog(
            ft.SnackBar(ft.Text(
                f"Analysed '{self.loaded_script_filename}' - {act_count} act(s), "
                f"{scene_count} scene(s), {character_count} character(s) found."
            ))
        )
        self.build_menu()

    def assign_voices(self, e):
        """Navigates to the voice-assignment screen."""
        self.menu_mode = 3
        self.build_menu()

    def assign_photos(self, e):
        """Navigates to the cast-photo screen."""
        self.menu_mode = 2
        self.build_menu()

    async def _assign_photo_for_role(self, role: "Role"):
        """Picks an image for ONE role, resizes it to icon size, and stores it."""
        filename, file_bytes, _full_path = await self._pick_file_bytes(
            dialog_title=f"Select a photo for {role.character}",
            tkinter_filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                ("All files", "*.*"),
            ],
            flet_file_type=ft.FilePickerFileType.IMAGE,
        )

        if filename is None:
            return  # cancelled, or _pick_file_bytes already showed an error

        try:
            icon_bytes = resize_image_to_icon(file_bytes)
        except Exception as ex:
            # Covers "picked a file that isn't actually a valid image"
            self.page.show_dialog(
                ft.SnackBar(ft.Text(f"Couldn't use '{filename}' as an image: {ex}"))
            )
            return

        role.photo = icon_bytes
        self._mark_dirty()
        self.page.show_dialog(ft.SnackBar(ft.Text(f"Photo set for {role.character}.")))
        self.build_menu()  # refresh the screen so the new thumbnail shows immediately

    def _resolve_role_photo_bytes(self, role: "Role") -> "bytes | None":
        """The actual PNG bytes to embed in the .llp for one role - ONLY if a real, user-chosen custom photo has been set.
        Critical: this must NEVER bake in a snapshot of the current gender-based default mask here."""
        return role.photo if role.photo else None

    def _tkinter_pick_save_path_sync(self, default_name: str) -> str:
        """Blocking helper, same reasoning and platform dispatch as _tkinter_pick_file_sync - see that function's own docstring for
        why Linux specifically routes to zenity rather than tkinter."""
        if self.page.platform == ft.PagePlatform.WINDOWS:
            return self._win32_pick_save_path_sync(default_name)
        if self.page.platform == ft.PagePlatform.LINUX:
            return self._zenity_pick_save_path_sync(default_name)

        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.asksaveasfilename(
            title="Save LineLearnerPro script",
            initialfile=default_name,
            defaultextension=".llp",
            filetypes=[("LineLearnerPro files", "*.llp"), ("All files", "*.*")],
        )
        root.destroy()
        return path

    def _zenity_pick_save_path_sync(self, default_name: str) -> str:
        """The Linux-native equivalent of _tkinter_pick_save_path_sync, via zenity - same reasoning as _zenity_pick_file_sync, including
        starting in and updating self._last_file_dialog_dir."""
        starting_path = default_name
        if self._last_file_dialog_dir:
            starting_path = str(Path(self._last_file_dialog_dir) / default_name)
        args = [
            "zenity", "--file-selection", "--save", "--confirm-overwrite",
            "--title", "Save LineLearnerPro script",
            "--filename", starting_path,
            "--file-filter", "LineLearnerPro files | *.llp",
            "--file-filter", "All files | *",
        ]
        try:
            result = subprocess.run(args, capture_output=True, text=True)
        except FileNotFoundError:
            return ""
        if result.returncode != 0:
            return ""
        path = result.stdout.strip()
        # zenity doesn't apply a defaultextension the way tkinter's asksaveasfilename does - add .llp ourselves if required
        if path and "." not in Path(path).name:
            path += ".llp"
        if path:
            self._last_file_dialog_dir = str(Path(path).parent)
        return path

    def _win32_pick_save_path_sync(self, default_name: str) -> str:
        """The Windows-native equivalent of _tkinter_pick_save_path_sync"""
        file_buffer = ctypes.create_unicode_buffer(default_name, 32768)
        filter_buffer = ctypes.create_unicode_buffer(
            _win32_file_filter_string([("LineLearnerPro files", "*.llp"), ("All files", "*.*")])
        )

        ofn = _OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
        ofn.hwndOwner = _win32_foreground_window()
        ofn.lpstrFilter = ctypes.cast(filter_buffer, wintypes.LPCWSTR)
        ofn.lpstrFile = ctypes.cast(file_buffer, wintypes.LPWSTR)
        ofn.nMaxFile = len(file_buffer)
        ofn.lpstrTitle = "Save LineLearnerPro script"
        ofn.lpstrDefExt = "llp"
        ofn.Flags = _OFN_EXPLORER | _OFN_OVERWRITEPROMPT | _OFN_NOCHANGEDIR

        if ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            return file_buffer.value
        return ""


    async def save_llp_script(self, e):
        """Normal case (desktop, script loaded via LoadtxtScript in the usual way): saves straight back to the SAME folder and base
        filename the .txt was loaded from, just with a .llp extension"""
        if not self.parsed_script:
            self.page.show_dialog(
                ft.SnackBar(ft.Text("Analyse a script first - there's nothing to save yet."))
            )
            return

        try:
            llp_bytes = build_llp_bundle(
                self.loaded_script_filename or "Untitled",
                self.parsed_script,
                self.roles,
                self._resolve_role_photo_bytes,
            )
        except Exception as ex:
            self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't build the .llp file: {ex}")))
            return

        default_name = Path(self.loaded_script_filename or "Untitled.txt").with_suffix(".llp").name

        if self.is_desktop:
            if self.loaded_script_path:
                output_path = self.loaded_script_path.with_suffix(".llp")
            else:
                loop = asyncio.get_event_loop()
                chosen = await loop.run_in_executor(
                    None, self._tkinter_pick_save_path_sync, default_name
                )
                if not chosen:
                    return  # user cancelled the dialog
                output_path = Path(chosen)

            try:
                output_path.write_bytes(llp_bytes)
            except OSError as ex:
                self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't save: {ex}")))
                return

            self._mark_saved(output_path.name, output_path)
            self.page.show_dialog(ft.SnackBar(ft.Text(f"Saved '{output_path.name}'.")))
            return

        # Web/mobile - no real filesystem "location" concept
        try:
            await self.file_picker.save_file(
                dialog_title="Save LineLearnerPro script",
                file_name=default_name,
                src_bytes=llp_bytes,
            )
        except Exception as ex:
            self.page.show_dialog(ft.SnackBar(ft.Text(f"Couldn't save: {ex}")))
            return

        self._mark_saved(default_name, None)  # no real path available on web/mobile

        self.page.show_dialog(ft.SnackBar(ft.Text(f"Saved '{default_name}'.")))

def main(page: ft.Page):
    LineLearnerProApp(page)

if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
