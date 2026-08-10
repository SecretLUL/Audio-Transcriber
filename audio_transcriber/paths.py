"""Central path resolution.

Fixes M1: the previous version used os.getcwd() everywhere. Launched from a
shortcut or a different working directory, settings.json and output/ ended up
somewhere else and bin/ counted as "missing" -> a 3 GB model re-download.
Every path now hangs off the script directory.
"""

import os

# .../Audio-Transcriber/audio_transcriber/paths.py  ->  .../Audio-Transcriber
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BIN_DIR = os.path.join(APP_DIR, "bin")
OUT_DIR = os.path.join(APP_DIR, "output")
TMP_DIR = os.path.join(OUT_DIR, ".tmp")
CFG_PATH = os.path.join(APP_DIR, "settings.json")
LOG_PATH = os.path.join(APP_DIR, "recorder.log")

WHISPER_EXE = os.path.join(BIN_DIR, "whisper-cli.exe")


def ensure_dirs():
    """Create the working directories (idempotent)."""
    for directory in (BIN_DIR, OUT_DIR, TMP_DIR):
        os.makedirs(directory, exist_ok=True)


def model_path(model_name):
    """Path to the ggml model file for e.g. 'small' or 'large-v3-turbo'."""
    return os.path.join(BIN_DIR, f"ggml-{model_name}.bin")


def safe_output_name(user_input, default="my_meeting"):
    """Turn user input into a safe base name without any path component.

    Blocks path traversal ('..\\..\\windows\\x') and empty names.
    """
    name = (user_input or "").strip()
    name = os.path.basename(name)
    name = os.path.splitext(name)[0]
    # Strip characters Windows does not allow in file names
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    name = name.strip(" .")
    return name or default
