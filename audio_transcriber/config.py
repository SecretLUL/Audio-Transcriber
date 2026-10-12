"""Settings: typed, versioned, with secure key storage.

Replaces the two ad-hoc dict methods of the previous version (save_settings /
load_settings), which silently swallowed every exception.
"""

import json
import os
from dataclasses import dataclass, asdict, field, fields, replace

from . import secretstore
from .paths import CFG_PATH

SCHEMA_VERSION = 2

# What settings.json holds for the cloud engines; a local model is stored
# under its whisper model name. The file used to hold a position in
# MODEL_CHOICES, so adding or moving an entry changed which model every saved
# file meant.
ELEVENLABS_MODEL = "elevenlabs"
STEPFUN_MODEL = "stepfun"
CLOUD_MODELS = (ELEVENLABS_MODEL, STEPFUN_MODEL)
DEFAULT_MODEL = "small"

# What the cloud engines are called in messages.
ENGINE_NAMES = {
    ELEVENLABS_MODEL: "ElevenLabs Scribe",
    STEPFUN_MODEL: "StepFun StepAudio 3 ASR",
}

# (display name, stored name)
MODEL_CHOICES = [
    ("ElevenLabs Scribe (Cloud API - state of the art)", ELEVENLABS_MODEL),
    ("StepFun StepAudio 3 ASR (Cloud API)", STEPFUN_MODEL),
    ("tiny (75 MB - very fast)", "tiny"),
    ("base (142 MB - fast)", "base"),
    ("small (466 MB - balanced)", "small"),
    ("medium (1.5 GB - slow)", "medium"),
    ("large-v3-turbo (1.5 GB - fast and accurate)", "large-v3-turbo"),
    ("large-v3 (3.1 GB - slow, highest accuracy)", "large-v3"),
]

# The list as it was while settings.json held a position in it (model_index).
# Files of that time are read with it, whatever the list holds now.
LEGACY_MODEL_ORDER = (ELEVENLABS_MODEL, "tiny", "base", "small", "medium",
                      "large-v3-turbo", "large-v3")


def model_keys():
    """The stored name of every entry of MODEL_CHOICES, in list order."""
    return [key for _label, key in MODEL_CHOICES]


def local_models():
    """The whisper models among MODEL_CHOICES, in list order."""
    return [key for key in model_keys() if key not in CLOUD_MODELS]


def model_key(index):
    """The stored name of the entry at `index` (clamped into the list)."""
    keys = model_keys()
    return keys[max(0, min(int(index), len(keys) - 1))]


def model_index(key):
    """Where a stored name sits in MODEL_CHOICES; the default's place if the
    name is not (or no longer) there."""
    keys = model_keys()
    for candidate in (key, DEFAULT_MODEL):
        if candidate in keys:
            return keys.index(candidate)
    return 0


# Automatic detection first, the languages alphabetically. Settings store the
# code, never a position, so the order can change freely.
LANGUAGE_CHOICES = [
    ("Detect automatically", "auto"),
    ("Arabic", "ar"), ("English", "en"), ("French", "fr"), ("German", "de"),
    ("Italian", "it"), ("Spanish", "es"), ("Turkish", "tr"),
]


@dataclass
class Settings:
    # --- Devices ----------------------------------------------------------
    mic_device: str = ""
    loop_device: str = ""

    # --- Levels (affect only the audible mixdown, NOT speaker attribution -
    #     see audit finding H2) --------------------------------------------
    mic_gain_db: float = 0.0
    loop_gain_db: float = 0.0

    # --- AI ---------------------------------------------------------------
    model: str = DEFAULT_MODEL      # a whisper model name or one of CLOUD_MODELS
    language: str = "de"
    whisper_threads: int = 0        # 0 = automatic
    # Silero VAD: off by default. Measured against a real recording, the VAD
    # path of this whisper.cpp build merges speech regions that are far apart
    # into a single segment (1.79 s - 41.83 s as one line) and loses the last
    # ~20 seconds. Accurate timestamps matter more for speaker attribution
    # than the small gain against hallucinations, which diarize.py filters
    # out energy-wise anyway.
    use_vad: bool = False
    # Let whisper.cpp use the GPU (through Vulkan). Off by
    # default because the Vulkan build crashed reproducibly on one AMD card;
    # with it on, a run that fails on the GPU is repeated on the CPU.
    use_gpu: bool = False
    # Recognise while recording, so stopping only has to catch up with the
    # tail. Not with ElevenLabs - see can_transcribe_live().
    live_transcribe: bool = True
    live_preview: bool = True       # show the running text while recording
    elevenlabs_model_id: str = "scribe_v2"
    stepfun_model_id: str = "stepaudio-3-asr-max"

    # --- Output -----------------------------------------------------------
    filename: str = "my_meeting"
    output_dir: str = ""            # empty = default paths.OUT_DIR
    separate_tracks: bool = True    # transcribe both tracks independently
    keep_raw_tracks: bool = False   # keep raw tracks after processing

    # --- Updates ----------------------------------------------------------
    # Ask GitHub for a newer release at every start (packaged builds only).
    check_for_updates: bool = True

    # --- Internal ---------------------------------------------------------
    schema_version: int = SCHEMA_VERSION
    elevenlabs_api_key_enc: str = ""
    stepfun_api_key_enc: str = ""

    # Runtime fields, never persisted in clear text: the ElevenLabs key and
    # the StepFun key
    api_key: str = field(default="", repr=False, compare=False)
    stepfun_api_key: str = field(default="", repr=False, compare=False)

    # Set by the loader when a plain-text key was migrated
    migrated_plaintext_key: bool = field(default=False, repr=False, compare=False)

    # ------------------------------------------------------------------
    def get_output_dir(self):
        """Returns the configured output directory or default OUT_DIR."""
        if self.output_dir and self.output_dir.strip():
            return os.path.abspath(self.output_dir.strip())
        from .paths import OUT_DIR
        return OUT_DIR

    def _model_key(self):
        return self.model if self.model in model_keys() else DEFAULT_MODEL

    def model_name(self):
        """whisper model name for the current choice, or None for cloud."""
        key = self._model_key()
        return None if key in CLOUD_MODELS else key

    def cloud_engine(self):
        """The chosen cloud engine - one of CLOUD_MODELS - or None."""
        key = self._model_key()
        return key if key in CLOUD_MODELS else None

    def uses_cloud(self):
        return self.cloud_engine() is not None

    def cloud_api_key(self):
        """The key the chosen cloud engine needs; "" for a local model."""
        return {ELEVENLABS_MODEL: self.api_key,
                STEPFUN_MODEL: self.stepfun_api_key}.get(self.cloud_engine(), "")

    def engine_name(self):
        """What a run recognises with, for messages: 'small', 'StepFun ...'."""
        return ENGINE_NAMES.get(self.cloud_engine()) or self.model_name()

    def can_transcribe_live(self):
        """Whether the recording can be transcribed while it runs.

        ElevenLabs cannot: every chunk would be a separate paid request, and
        the final transcript has to come from one and the same recogniser -
        mixing whisper chunks with an ElevenLabs tail would show in the text.
        StepFun is sent passage by passage anyway, so a chunk costs nothing
        extra and the closing pass uses the same recogniser.
        """
        return self.cloud_engine() != ELEVENLABS_MODEL

    def snapshot(self):
        """An independent copy for a worker thread.

        The window keeps changing the live object while a run is going on: a
        gain slider moves it, 'Save settings' rewrites it from the widgets. A
        worker that read it directly could change model or language half way
        through a transcript.
        """
        return replace(self)

    def live_model_name(self):
        """Model used for the live preview.

        Fixes H9: the previous version ran the preview with the full user
        model - possibly large-v3 on the CPU, every 3 seconds, over the entire
        recording so far. The preview now always uses a small model; final
        quality comes from the closing pass.
        """
        chosen = self.model_name()
        if chosen in ("tiny", "base", "small"):
            return chosen
        return "small"

    def threads(self):
        if self.whisper_threads > 0:
            return self.whisper_threads
        # Measured on the development machine: small -t 4 = 15.6 s,
        # -t 10 = 10.7 s. Two cores stay free for the GUI and audio threads.
        return max(1, min((os.cpu_count() or 4) - 2, 12))


# ----------------------------------------------------------------------
# Every API key: the runtime field holding it, the field storing it encrypted,
# and its secretstore service (DPAPI entropy, environment variable).
_KEYS = (
    ("api_key", "elevenlabs_api_key_enc", ELEVENLABS_MODEL),
    ("stepfun_api_key", "stepfun_api_key_enc", STEPFUN_MODEL),
)
_RUNTIME_FIELDS = ("api_key", "stepfun_api_key", "migrated_plaintext_key")


def load(path=None):
    """Load the settings. Returns (Settings, warnings).

    Invalid individual values are dropped instead of invalidating the whole
    file; the previous version silently reset everything to defaults on any
    error.

    `path` defaults to CFG_PATH as it is when called. It used to be bound as a
    default argument at import time, so a test that pointed config.CFG_PATH at
    a temporary file ("never touch the user's real settings") changed nothing:
    the real settings.json was still read - and, on save, overwritten.
    """
    path = CFG_PATH if path is None else path
    warnings = []
    settings = Settings()

    if not os.path.exists(path):
        _keys_from_environment(settings)
        return settings, warnings

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception as exc:
        warnings.append(f"settings.json could not be read ({exc}); "
                        f"falling back to defaults.")
        _keys_from_environment(settings)
        return settings, warnings

    if not isinstance(raw, dict):
        warnings.append("settings.json has an unexpected format.")
        _keys_from_environment(settings)
        return settings, warnings

    known = {f.name: f.type for f in fields(Settings)}
    for key, value in raw.items():
        if key not in known or key in _RUNTIME_FIELDS:
            continue
        try:
            current = getattr(settings, key)
            if isinstance(current, bool):
                setattr(settings, key, bool(value))
            elif isinstance(current, int):
                setattr(settings, key, int(value))
            elif isinstance(current, float):
                setattr(settings, key, float(value))
            else:
                setattr(settings, key, str(value))
        except (TypeError, ValueError):
            warnings.append(f"Setting '{key}' was invalid and has been ignored.")

    _read_model(settings, raw, warnings)
    settings.mic_gain_db = max(-40.0, min(40.0, settings.mic_gain_db))
    settings.loop_gain_db = max(-40.0, min(40.0, settings.loop_gain_db))

    # --- Obtain the keys -----------------------------------------------
    for attribute, stored, service in _KEYS:
        setattr(settings, attribute,
                secretstore.decrypt(getattr(settings, stored), service))

    # Migration: adopt a plain-text key from schema v1 and warn about it
    legacy = str(raw.get("elevenlabs_api_key", "")).strip()
    if legacy and not settings.api_key:
        settings.api_key = legacy
        settings.migrated_plaintext_key = True
        warnings.append(
            "The API key was stored in clear text in settings.json. It has been "
            "adopted and will be encrypted the next time you save. "
            "IMPORTANT: revoke that key in the ElevenLabs dashboard and issue "
            "a new one - the old value sat unprotected on disk."
        )

    _keys_from_environment(settings)
    return settings, warnings


def _keys_from_environment(settings):
    """Take every key that is not set from its environment variable."""
    for attribute, _stored, service in _KEYS:
        if not getattr(settings, attribute):
            setattr(settings, attribute, secretstore.from_environment(service))


def _read_model(settings, raw, warnings):
    """Settle `settings.model`: a stored name, or the position of older files."""
    if "model" not in raw and "model_index" in raw:
        try:
            index = int(raw["model_index"])
            settings.model = LEGACY_MODEL_ORDER[
                max(0, min(index, len(LEGACY_MODEL_ORDER) - 1))]
        except (TypeError, ValueError):
            warnings.append("Setting 'model_index' was invalid and has been ignored.")
    if settings.model not in model_keys():
        warnings.append(f"The model '{settings.model}' in settings.json is not "
                        f"known; using {DEFAULT_MODEL}.")
        settings.model = DEFAULT_MODEL


def save(settings, path=None):
    """Save atomically. The keys are never written in clear text.

    A key that merely came from its environment variable (ELEVENLABS_API_KEY,
    STEPFUN_API_KEY) is not written at all: the stored copy would win over the
    variable from then on, so rotating the variable would silently change
    nothing.

    Returns a list of warnings, empty when everything was stored. A key that
    could not be secured is left out of the file - and that has to be said: it
    used to vanish at the next start without a word.
    """
    path = CFG_PATH if path is None else path
    warnings = []
    data = asdict(settings)
    for name in _RUNTIME_FIELDS:
        data.pop(name, None)
    data["schema_version"] = SCHEMA_VERSION

    for attribute, stored, service in _KEYS:
        key = getattr(settings, attribute)
        data[stored] = ""
        if not key or key == secretstore.from_environment(service):
            continue
        try:
            data[stored] = secretstore.encrypt(key, service)
        except OSError as exc:
            # Store nothing rather than clear text.
            warnings.append(f"The {ENGINE_NAMES[service]} API key could not be "
                            f"encrypted ({exc}) and was not saved. It stays in "
                            f"use until you close the app.")

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=4, ensure_ascii=False)
    os.replace(tmp_path, path)   # atomic: never a half-written settings.json
    return warnings
