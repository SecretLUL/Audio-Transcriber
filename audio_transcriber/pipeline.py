"""What happens after stop: prepare tracks, transcribe, build the transcript.

Fixes K1, K4 and H5:
  * The saved transcript ALWAYS comes from a complete closing pass. Live
    transcription is a preview only. In the previous version the live text was
    written as the final result, which made the ElevenLabs path unreachable
    (K1) and cut the end off the transcript (K4: measured 16.6 % on a real
    recording).
  * All post-processing ran on the GUI thread. It now runs in a worker and the
    interface stays responsive.
"""

import os
import threading

import numpy as np
import soundfile as sf

from . import diarize
from .audio import capture, dsp
from .events import Failed, Finished, Log, Progress, Status
from .paths import OUT_DIR, TMP_DIR
from .transcribe.base import TranscriptionError
from .transcribe.elevenlabs import ElevenLabsBackend
from .transcribe.whispercpp import WhisperCppBackend


def build_backend(settings, greedy=False, live=False):
    """Create the backend matching the current selection."""
    if settings.uses_cloud() and not live:
        return ElevenLabsBackend(api_key=settings.api_key,
                                 model_id=settings.elevenlabs_model_id)
    model = settings.live_model_name() if live else settings.model_name()
    if model is None:
        model = settings.live_model_name()
    return WhisperCppBackend(
        model_name=model,
        threads=settings.threads(),
        use_vad=settings.use_vad,
        greedy=greedy,
    )


class Finalizer:
    """Runs the post-processing of a recording in a worker thread."""

    def __init__(self, bridge, settings, backend_factory=None):
        self.bridge = bridge
        self.settings = settings
        # Injectable so the flow can be tested without a real AI backend.
        self.backend_factory = backend_factory or (lambda s: build_backend(s))
        self._backends = []
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()
        for backend in list(self._backends):
            try:
                backend.cancel()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def run_async(self, recording, base_name):
        thread = threading.Thread(target=self._run, args=(recording, base_name),
                                  name="finalize", daemon=True)
        thread.start()
        return thread

    def _run(self, recording, base_name):
        try:
            self._process(recording, base_name)
        except TranscriptionError as exc:
            self.bridge.post(Failed(message=str(exc)))
        except Exception as exc:                      # last line of defence
            self.bridge.post_exception("Unexpected error during processing", exc)

    # ------------------------------------------------------------------
    def _process(self, recording, base_name):
        bridge, settings = self.bridge, self.settings
        os.makedirs(OUT_DIR, exist_ok=True)

        for warning in recording.warnings:
            bridge.post(Log(f"Note: {warning}\n"))

        bridge.post(Status("Preparing audio tracks…", "orange"))

        # --- 1. Load raw tracks, align them, resample to 16 kHz ---------
        mic = capture.load_track(recording.mic)
        system = capture.load_track(recording.sys)

        if len(mic) == 0 and len(system) == 0:
            raise TranscriptionError("No audio data was captured.")

        length = max(len(mic), len(system))
        mic = _pad_to(mic, length)
        system = _pad_to(system, length)

        # --- 2. Audible mixdown (with the gain sliders) -----------------
        mix_path = os.path.join(OUT_DIR, f"{base_name}.wav")
        stereo = np.column_stack([
            dsp.limit_peak(dsp.apply_gain(mic, settings.mic_gain_db)),
            dsp.limit_peak(dsp.apply_gain(system, settings.loop_gain_db)),
        ])
        sf.write(mix_path, stereo, dsp.TARGET_RATE, subtype="PCM_16")
        bridge.post(Log(f"Recording saved: {os.path.basename(mix_path)} "
                        f"({length / dsp.TARGET_RATE:.1f} s)\n"))

        # --- 3. Normalise the tracks for recognition --------------------
        # Independent normalisation is safe now: speaker attribution works on
        # levels relative to each track.
        os.makedirs(TMP_DIR, exist_ok=True)
        asr_paths = {}
        for kind, audio in (("mic", mic), ("sys", system)):
            if len(audio) == 0 or dsp.reference_level(audio) <= dsp.SILENCE_FLOOR:
                continue
            path = os.path.join(TMP_DIR, f"{base_name}.{kind}.asr.wav")
            sf.write(path, dsp.normalize_for_asr(audio), dsp.TARGET_RATE,
                     subtype="PCM_16")
            asr_paths[kind] = path

        if not asr_paths:
            raise TranscriptionError(
                "Both tracks are practically silent - there is nothing to "
                "transcribe. Are the right devices selected and is system "
                "audio actually playing?")

        # --- 4. Transcription -------------------------------------------
        segments = {"mic": [], "sys": []}
        if settings.separate_tracks:
            for kind, path in asr_paths.items():
                if self._cancelled.is_set():
                    raise TranscriptionError("Processing cancelled.")
                label = "your track" if kind == "mic" else "the system track"
                bridge.post(Status(f"Transcribing {label}…", "purple"))
                bridge.post(Log(f"\n--- Transcription: {label} ---\n"))
                segments[kind] = self._transcribe(path, kind)
        else:
            # Single pass over the mixdown (faster, but the attribution has to
            # be estimated again).
            bridge.post(Status("Transcribing mixdown…", "purple"))
            merged_path = os.path.join(TMP_DIR, f"{base_name}.mixdown.asr.wav")
            sf.write(merged_path, dsp.normalize_for_asr((mic + system) * 0.5),
                     dsp.TARGET_RATE, subtype="PCM_16")
            for segment in self._transcribe(merged_path, "mic"):
                own = dsp.segment_rms(mic, segment.start, segment.end)
                other = dsp.segment_rms(system, segment.start, segment.end)
                segments["mic" if own >= other else "sys"].append(segment)

        # --- 5. Merge the tracks ----------------------------------------
        bridge.post(Status("Building transcript…", "orange"))
        report = diarize.merge(segments["mic"], segments["sys"],
                               mic_audio=mic, sys_audio=system)
        summary = report.summary()
        if summary:
            bridge.post(Log(f"\nQuality filter: {summary}.\n"))

        text = diarize.render(report)
        if not text.strip():
            text = "[No spoken text was recognised.]"

        txt_path = os.path.join(OUT_DIR, f"{base_name}.txt")
        with open(txt_path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

        # --- 6. Clean up -------------------------------------------------
        if not settings.keep_raw_tracks:
            for track in (recording.mic, recording.sys):
                if track and os.path.exists(track.path):
                    _try_remove(track.path)
            for path in asr_paths.values():
                _try_remove(path)

        bridge.post(Finished(text=text, txt_path=txt_path, audio_path=mix_path))

    # ------------------------------------------------------------------
    def _transcribe(self, path, kind):
        backend = self.backend_factory(self.settings)
        self._backends.append(backend)
        try:
            return backend.transcribe(
                path,
                language=self.settings.language,
                log=lambda message: self.bridge.post(Log(message)),
                track=kind,
                progress=lambda message: self.bridge.post(Progress(message)),
            )
        finally:
            if backend in self._backends:
                self._backends.remove(backend)


# ----------------------------------------------------------------------
class FileFinalizer:
    """Runs post-processing and transcription for an uploaded audio file in a worker thread."""

    def __init__(self, bridge, settings, backend_factory=None):
        self.bridge = bridge
        self.settings = settings
        self.backend_factory = backend_factory or (lambda s: build_backend(s))
        self._backends = []
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()
        for backend in list(self._backends):
            try:
                backend.cancel()
            except Exception:
                pass

    def run_async(self, file_path, base_name=None):
        if not base_name:
            base_name = paths.safe_output_name(os.path.splitext(os.path.basename(file_path))[0])
        thread = threading.Thread(target=self._run, args=(file_path, base_name),
                                  name="file-finalize", daemon=True)
        thread.start()
        return thread

    def _run(self, file_path, base_name):
        try:
            self._process(file_path, base_name)
        except TranscriptionError as exc:
            self.bridge.post(Failed(message=str(exc)))
        except Exception as exc:
            self.bridge.post_exception("Unexpected error during file processing", exc)

    def _process(self, file_path, base_name):
        bridge, settings = self.bridge, self.settings
        os.makedirs(OUT_DIR, exist_ok=True)
        os.makedirs(TMP_DIR, exist_ok=True)

        bridge.post(Status("Loading audio file…", "orange"))
        bridge.post(Log(f"Opening audio file: {os.path.basename(file_path)}…\n"))

        from .audio.loader import load_audio_file
        audio = load_audio_file(file_path, target_rate=dsp.TARGET_RATE)

        if len(audio) == 0 or dsp.reference_level(audio) <= dsp.SILENCE_FLOOR:
            raise TranscriptionError("The selected audio file is silent or contains no audible speech.")

        # Save audio file to output folder as 16 kHz PCM WAV
        mix_path = os.path.join(OUT_DIR, f"{base_name}.wav")
        sf.write(mix_path, dsp.limit_peak(audio), dsp.TARGET_RATE, subtype="PCM_16")
        duration_s = len(audio) / float(dsp.TARGET_RATE)
        bridge.post(Log(f"Audio file loaded: {os.path.basename(mix_path)} ({duration_s:.1f} s)\n"))

        # Save normalized WAV into TMP_DIR for ASR
        asr_path = os.path.join(TMP_DIR, f"{base_name}.file.asr.wav")
        sf.write(asr_path, dsp.normalize_for_asr(audio), dsp.TARGET_RATE, subtype="PCM_16")

        if self._cancelled.is_set():
            raise TranscriptionError("Processing cancelled.")

        bridge.post(Status("Transcribing audio file…", "purple"))
        bridge.post(Log("\n--- Transcription ---\n"))
        segments = self._transcribe(asr_path, "file")

        _try_remove(asr_path)

        if self._cancelled.is_set():
            raise TranscriptionError("Processing cancelled.")

        bridge.post(Status("Building transcript…", "orange"))
        from .transcribe.base import format_timestamp
        lines = []
        for seg in segments:
            speaker_str = f" {seg.speaker_hint}:" if getattr(seg, "speaker_hint", "") else ""
            lines.append(f"{format_timestamp(seg.start)}{speaker_str} {seg.text}")

        text = "\n".join(lines)
        if not text.strip():
            text = "[No spoken text was recognised.]"

        txt_path = os.path.join(OUT_DIR, f"{base_name}.txt")
        with open(txt_path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

        bridge.post(Finished(text=text, txt_path=txt_path, audio_path=mix_path))

    def _transcribe(self, path, kind):
        backend = self.backend_factory(self.settings)
        self._backends.append(backend)
        try:
            return backend.transcribe(
                path,
                language=self.settings.language,
                log=lambda message: self.bridge.post(Log(message)),
                track=kind,
                progress=lambda message: self.bridge.post(Progress(message)),
            )
        finally:
            if backend in self._backends:
                self._backends.remove(backend)


# ----------------------------------------------------------------------
class LivePreview:
    """Continuously transcribes only the most recent time window.

    Fixes K4: the previous version re-transcribed the entire recording so far
    on every pass (quadratic effort). Cost is constant here because only a
    30 second window is processed.
    """

    def __init__(self, bridge, settings, engine, interval_s=6.0):
        self.bridge = bridge
        self.settings = settings
        self.engine = engine
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._backend = None
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="live-preview",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._backend is not None:
            try:
                self._backend.cancel()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _loop(self):
        preview_path = os.path.join(TMP_DIR, ".live_preview.wav")
        os.makedirs(TMP_DIR, exist_ok=True)
        self._backend = build_backend(self.settings, greedy=True, live=True)

        try:
            self._backend.prepare(log=lambda m: self.bridge.post(Log(m)))
        except Exception as exc:
            self.bridge.post(Log(f"Live preview unavailable: {exc}\n"))
            return

        while not self._stop.wait(self.interval_s):
            try:
                mic, system = self.engine.live_window()
                if mic is None and system is None:
                    continue
                length = max(len(mic or []), len(system or []))
                if length < dsp.TARGET_RATE * 2:      # less than 2 s of material
                    continue

                mic = _pad_to(mic if mic is not None else np.zeros(0, np.float32), length)
                system = _pad_to(system if system is not None else np.zeros(0, np.float32),
                                 length)
                mono = dsp.normalize_for_asr((mic + system) * 0.5)
                sf.write(preview_path, mono, dsp.TARGET_RATE, subtype="PCM_16")

                segments = self._backend.transcribe(
                    preview_path, language=self.settings.language, track="mic")
                if self._stop.is_set():
                    break

                lines = []
                for segment in segments:
                    own = dsp.segment_rms(mic, segment.start, segment.end)
                    other = dsp.segment_rms(system, segment.start, segment.end)
                    label = diarize.LABEL_SELF if own >= other else diarize.LABEL_OTHER
                    lines.append(f"{label}: {segment.text}")
                if lines:
                    self.bridge.post(_preview_event("\n".join(lines)))
            except Exception:
                # The preview must never endanger the recording.
                continue

        _try_remove(preview_path)


def _preview_event(text):
    from .events import LivePreview as LivePreviewEvent
    return LivePreviewEvent(text=text)


# ----------------------------------------------------------------------
def _pad_to(audio, length):
    audio = np.asarray(audio, dtype=np.float32)
    if len(audio) >= length:
        return audio[:length]
    return np.concatenate([audio, np.zeros(length - len(audio), dtype=np.float32)])


def _try_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
