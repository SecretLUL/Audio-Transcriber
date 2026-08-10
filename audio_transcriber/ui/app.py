"""The main window.

Layout: a header, then cards (Sources, Transcription, Recording, Transcript).
All colours and spacings come from theme.py, the controls from widgets.py.

The interface still contains no audio, network or process logic. It reads
widgets exclusively on the GUI thread, hands snapshots to the workers and
receives results only through UiBridge events.
"""

import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import pyaudiowpatch as pyaudio

from .. import config, paths, pipeline
from ..audio import devices as devmod
from ..audio.capture import AudioEngine
from ..events import (Failed, Finished, LivePreview, Log, Progress, Status,
                      UiBridge)
from . import theme as T
from . import widgets as W

METER_INTERVAL_MS = 40


class RecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Audio AI Recorder")
        self.root.geometry("800x960")
        # Below this height there would be no usable space left for the
        # transcript - the cards above need about 750 px together.
        self.root.minsize(740, 900)

        T.apply(root)

        self.settings, warnings = config.load()
        self.pa = pyaudio.PyAudio()
        self.engine = AudioEngine(self.pa, paths.TMP_DIR)
        self.bridge = UiBridge(root)
        self.devices = []
        self.finalizer = None
        self.live_preview = None
        self.recording_base_name = None
        self.recording_started_at = None
        self._shutting_down = False
        self._meter_after_id = None

        paths.ensure_dirs()
        self._build()
        self._wire_events()
        self.bridge.start()

        for warning in warnings:
            self.transcript.append(f"⚠ {warning}\n")
        if self.settings.migrated_plaintext_key:
            self.transcript.append(
                "→ The key will be stored encrypted the next time you save.\n\n")

        self.refresh_devices()
        self._tick()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<F5>", lambda _e: self._toggle_recording())

    # ==================================================================
    # Construction
    # ==================================================================
    def _build(self):
        outer = tk.Frame(self.root, bg=T.BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=T.XL, pady=(T.LG, T.LG))

        self._build_header(outer)
        self._build_sources(outer)
        self._build_ai(outer)
        self._build_record_bar(outer)
        self._build_transcript(outer)

    # ------------------------------------------------------------------
    def _build_header(self, parent):
        head = tk.Frame(parent, bg=T.BG)
        head.pack(fill=tk.X, pady=(0, T.MD))

        left = tk.Frame(head, bg=T.BG)
        left.pack(side=tk.LEFT)
        tk.Label(left, text="Audio AI Recorder", bg=T.BG, fg=T.TEXT,
                 font=T.fonts["display"], anchor="w").pack(anchor="w")
        self.subtitle = tk.Label(
            left, bg=T.BG, fg=T.TEXT_MUTE, font=T.fonts["small"], anchor="w",
            text=f"whisper.cpp · {self.settings.threads()} threads · "
                 f"ElevenLabs Scribe")
        self.subtitle.pack(anchor="w", pady=(2, 0))

        self.status = W.StatusPill(head, bg=T.BG, width=260, height=30)
        self.status.pack(side=tk.RIGHT, anchor="e")

    # ------------------------------------------------------------------
    def _build_sources(self, parent):
        card = W.Card(parent, title="Sources", hint="")
        card.pack(fill=tk.X, pady=(0, T.SM))
        self.sources_card = card
        body = card.body

        self.mic_combo, self.mic_meter, self.mic_gain, self.mic_gain_label = \
            self._source_row(body, "🎤", "Microphone — your voice",
                             self._on_mic_gain, row=0)
        tk.Frame(body, bg=T.BORDER, height=1).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=T.SM)
        self.sys_combo, self.sys_meter, self.sys_gain, self.sys_gain_label = \
            self._source_row(body, "🔊", "Playback — the other voices",
                             self._on_sys_gain, row=2)

        body.columnconfigure(1, weight=1)

    def _source_row(self, body, icon, label, gain_command, row):
        head = tk.Frame(body, bg=T.CARD)
        head.grid(row=row, column=0, columnspan=3, sticky="ew")
        head.columnconfigure(1, weight=1)

        tk.Label(head, text=icon, bg=T.CARD, fg=T.TEXT,
                 font=T.fonts["title"]).grid(row=0, column=0, sticky="w",
                                             padx=(0, T.SM))
        tk.Label(head, text=label, bg=T.CARD, fg=T.TEXT_DIM,
                 font=T.fonts["small"], anchor="w").grid(row=0, column=1,
                                                         sticky="w")

        combo = ttk.Combobox(head, state="readonly", style="Dark.TCombobox",
                             font=T.fonts["body"])
        combo.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 6))
        combo.bind("<<ComboboxSelected>>", lambda _e: self.restart_monitoring())

        meter = W.Meter(head, width=560, height=14)
        meter.grid(row=2, column=0, columnspan=3, sticky="ew")

        gain_row = tk.Frame(head, bg=T.CARD)
        gain_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        gain_row.columnconfigure(1, weight=1)
        tk.Label(gain_row, text="Gain", bg=T.CARD, fg=T.TEXT_MUTE,
                 font=T.fonts["tiny"], width=5, anchor="w").grid(row=0, column=0)
        slider = W.Slider(gain_row, from_=-20.0, to=20.0, command=gain_command,
                          width=480)
        slider.grid(row=0, column=1, sticky="ew")
        value = tk.Label(gain_row, text="+0.0 dB", bg=T.CARD, fg=T.TEXT_DIM,
                         font=T.fonts["mono_small"], width=9, anchor="e")
        value.grid(row=0, column=2, sticky="e")
        return combo, meter, slider, value

    # ------------------------------------------------------------------
    def _build_ai(self, parent):
        card = W.Card(parent, title="Transcription")
        card.pack(fill=tk.X, pady=(0, T.SM))
        body = card.body
        body.columnconfigure(0, weight=3, uniform="ai")
        body.columnconfigure(1, weight=2, uniform="ai")

        model_field = W.Field(body, "Model", lambda p: _combo(
            p, [choice[0] for choice in config.MODEL_CHOICES]))
        model_field.grid(row=0, column=0, sticky="ew", padx=(0, T.MD))
        self.model_combo = model_field.widget
        self.model_combo.current(self.settings.model_index)
        self.model_combo.bind("<<ComboboxSelected>>",
                              lambda _e: self._refresh_key_state())

        lang_field = W.Field(body, "Language", lambda p: _combo(
            p, [choice[0] for choice in config.LANGUAGE_CHOICES]))
        lang_field.grid(row=0, column=1, sticky="ew")
        self.lang_combo = lang_field.widget
        self.lang_combo.current(_index_of(config.LANGUAGE_CHOICES,
                                          self.settings.language))

        # --- API key ---------------------------------------------------
        key_wrap = tk.Frame(body, bg=T.CARD)
        key_wrap.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(T.MD, 0))
        key_wrap.columnconfigure(0, weight=1)

        self.key_field = W.Field(
            key_wrap, "ElevenLabs API key",
            lambda p: ttk.Entry(p, show="•", style="Dark.TEntry",
                                font=T.fonts["body"]),
            hint="Encrypted with the Windows DPAPI and bound to your user "
                 "account — never stored in clear text.")
        self.key_field.grid(row=0, column=0, sticky="ew")
        self.api_entry = self.key_field.widget
        self.api_entry.insert(0, self.settings.api_key)

        self.show_key_btn = W.Button(key_wrap, text="show", kind="ghost",
                                     width=78, height=32,
                                     command=self._toggle_key)
        self.show_key_btn.grid(row=0, column=1, sticky="n",
                               padx=(T.SM, 0), pady=(22, 0))
        self._key_visible = False

        # --- Options ---------------------------------------------------
        options = tk.Frame(body, bg=T.CARD)
        options.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(T.MD, 0))

        self.live_var = tk.BooleanVar(value=self.settings.live_transcribe)
        self.separate_var = tk.BooleanVar(value=self.settings.separate_tracks)
        self.vad_var = tk.BooleanVar(value=self.settings.use_vad)

        W.Switch(options, "Live preview", self.live_var).grid(
            row=0, column=0, sticky="w")
        W.Switch(options, "Separate tracks", self.separate_var).grid(
            row=0, column=1, sticky="w", padx=(T.LG, 0))
        W.Switch(options, "VAD", self.vad_var).grid(
            row=0, column=2, sticky="w", padx=(T.LG, 0))

        self.save_btn = W.Button(options, text="Save settings",
                                 kind="ghost", width=178, height=32,
                                 command=self.save_settings)
        self.save_btn.grid(row=0, column=3, sticky="e")
        options.columnconfigure(3, weight=1)

    # ------------------------------------------------------------------
    def _build_record_bar(self, parent):
        card = W.Card(parent)
        card.pack(fill=tk.X, pady=(0, T.SM))
        body = card.body
        body.columnconfigure(0, weight=1)

        name_field = W.Field(body, "File name", lambda p: ttk.Entry(
            p, style="Dark.TEntry", font=T.fonts["body"]))
        name_field.grid(row=0, column=0, sticky="ew", padx=(0, T.LG))
        self.filename_entry = name_field.widget
        self.filename_entry.insert(0, self.settings.filename)

        self.timer_label = tk.Label(body, text="00:00", bg=T.CARD,
                                    fg=T.TEXT_MUTE, font=T.fonts["display"])
        self.timer_label.grid(row=0, column=1, sticky="s", padx=(0, T.LG),
                              pady=(0, 2))

        self.start_btn = W.Button(body, text="Start recording", icon="⏺",
                                  kind="record", width=190, height=42,
                                  command=self.start_recording)
        self.start_btn.grid(row=0, column=2, sticky="s", pady=(0, 1))

        self.stop_btn = W.Button(body, text="Stop", icon="⏹", kind="stop",
                                 width=130, height=42, state="disabled",
                                 command=self.stop_recording)
        self.stop_btn.grid(row=0, column=3, sticky="s", padx=(T.SM, 0),
                           pady=(0, 1))

    # ------------------------------------------------------------------
    def _build_transcript(self, parent):
        card = W.Card(parent, title="Transcript", stretch=True)
        card.pack(fill=tk.BOTH, expand=True)
        self.transcript = W.Transcript(card.body)
        self.transcript.pack(fill=tk.BOTH, expand=True)

    # ==================================================================
    # Events
    # ==================================================================
    def _wire_events(self):
        self.bridge.on(Log, lambda e: self.transcript.append(e.text))
        self.bridge.on(Progress, lambda e: self.transcript.replace_last_line(e.text))
        self.bridge.on(Status, lambda e: self.status.set(e.text, _colour(e.color)))
        self.bridge.on(LivePreview, self._on_live_preview)
        self.bridge.on(Finished, self._on_finished)
        self.bridge.on(Failed, self._on_failed)

    def _on_live_preview(self, event):
        self.transcript.set_transcript(
            event.text,
            header="⚡ Live preview of the last 30 seconds — "
                   "the final transcript is produced when you stop.")

    def _on_finished(self, event):
        self.transcript.set_transcript(event.text)
        self.status.set("done · transcript saved", T.OK)
        self._reset_controls()
        messagebox.showinfo(
            "Done",
            f"Transcription complete.\n\n"
            f"Audio:      {os.path.basename(event.audio_path)}\n"
            f"Transcript: {os.path.basename(event.txt_path)}\n\n"
            f"Folder: {paths.OUT_DIR}")

    def _on_failed(self, event):
        self.transcript.append(f"\n[ERROR] {event.message}\n")
        self.status.set("processing failed", T.DANGER)
        self._reset_controls()
        messagebox.showerror("Error", event.message)

    # ==================================================================
    # Devices
    # ==================================================================
    def refresh_devices(self):
        self.devices = devmod.enumerate_devices(self.pa)
        mics = devmod.microphone_candidates(self.devices)
        outputs = (devmod.playback_candidates(self.devices)
                   + devmod.loopback_devices(self.devices))

        self.mic_combo["values"] = [device.label for device in mics]
        self.sys_combo["values"] = [device.label for device in outputs]

        if not mics:
            self.transcript.append("⚠ No capture device was found.\n")
        if not outputs:
            self.transcript.append("⚠ No playback device was found.\n")

        self._select(self.mic_combo, mics, self.settings.mic_device)
        self._select(self.sys_combo, outputs, self.settings.loop_device)

        self.mic_gain.set(self.settings.mic_gain_db)
        self._on_mic_gain(self.settings.mic_gain_db)
        self.sys_gain.set(self.settings.loop_gain_db)
        self._on_sys_gain(self.settings.loop_gain_db)

        self._refresh_key_state()
        self.restart_monitoring()

    @staticmethod
    def _select(combo, device_list, preferred):
        if not device_list:
            return
        labels = [device.label for device in device_list]
        if preferred in labels:
            combo.set(preferred)
            return
        if preferred and ":" in preferred:
            wanted = preferred.split(":", 1)[1].strip()
            for index, device in enumerate(device_list):
                if device.name == wanted:
                    combo.current(index)
                    return
        combo.current(0)

    def _current_devices(self):
        mic = devmod.by_label(self.devices, self.mic_combo.get())
        chosen = devmod.by_label(self.devices, self.sys_combo.get())
        loopback, reason = devmod.find_loopback_for(self.devices, chosen)
        return mic, loopback, reason

    def restart_monitoring(self):
        if self.engine.is_recording:
            return
        mic, loopback, reason = self._current_devices()
        self.sources_card.set_hint(
            f"System audio captured from: {reason}" if loopback else f"⚠ {reason}")

        def worker():
            for warning in self.engine.configure(mic, loopback):
                self.bridge.post(Log(f"⚠ {warning}\n"))

        threading.Thread(target=worker, name="restart-monitor", daemon=True).start()

    # ==================================================================
    # Recording
    # ==================================================================
    def _toggle_recording(self):
        if self.engine.is_recording:
            self.stop_recording()
        elif str(self.start_btn["state"]) != "disabled":
            self.start_recording()

    def start_recording(self):
        mic, loopback, _reason = self._current_devices()
        if mic is None and loopback is None:
            messagebox.showwarning("No devices",
                                   "Please select a microphone and a playback device.")
            return

        self._sync_settings_from_ui()
        base_name = paths.safe_output_name(self.filename_entry.get())
        self.filename_entry.delete(0, tk.END)
        self.filename_entry.insert(0, base_name)

        try:
            self.engine.start_recording(base_name)
        except RuntimeError as exc:
            messagebox.showerror("Cannot record", str(exc))
            return

        self.recording_base_name = base_name
        self.recording_started_at = time.monotonic()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.mic_combo.config(state="disabled")
        self.sys_combo.config(state="disabled")
        self.timer_label.config(fg=T.REC)
        self.status.set("recording", T.REC, pulse=True)
        self.transcript.clear()
        self.transcript.append("Recording started.\n")

        if self.live_var.get():
            self.live_preview = pipeline.LivePreview(self.bridge, self.settings,
                                                     self.engine)
            self.live_preview.start()

    def stop_recording(self):
        self.stop_btn.config(state="disabled")
        self.status.set("stopping…", T.WARN)
        self.recording_started_at = None
        self.timer_label.config(fg=T.TEXT_MUTE)

        if self.live_preview is not None:
            self.live_preview.stop()
            self.live_preview = None

        recording = self.engine.stop_recording()
        if not recording.has_audio:
            messagebox.showwarning("No data", "No audio data was captured.")
            self._reset_controls()
            return

        self.finalizer = pipeline.Finalizer(self.bridge, self.settings)
        self.finalizer.run_async(recording, self.recording_base_name)

    def _reset_controls(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.mic_combo.config(state="readonly")
        self.sys_combo.config(state="readonly")
        self.recording_started_at = None
        self.timer_label.config(text="00:00", fg=T.TEXT_MUTE)
        self.mic_meter.reset()
        self.sys_meter.reset()

    # ==================================================================
    # Settings
    # ==================================================================
    def _sync_settings_from_ui(self):
        settings = self.settings
        settings.mic_device = self.mic_combo.get()
        settings.loop_device = self.sys_combo.get()
        settings.mic_gain_db = round(float(self.mic_gain.get()), 1)
        settings.loop_gain_db = round(float(self.sys_gain.get()), 1)
        settings.model_index = max(0, self.model_combo.current())
        settings.language = config.LANGUAGE_CHOICES[max(0, self.lang_combo.current())][1]
        settings.api_key = self.api_entry.get().strip()
        settings.live_transcribe = bool(self.live_var.get())
        settings.separate_tracks = bool(self.separate_var.get())
        settings.use_vad = bool(self.vad_var.get())
        settings.filename = paths.safe_output_name(self.filename_entry.get())

    def save_settings(self):
        self._sync_settings_from_ui()
        try:
            config.save(self.settings)
        except Exception as exc:
            messagebox.showerror("Error", f"Settings could not be saved:\n{exc}")
            return
        self.settings.migrated_plaintext_key = False
        self.status.set("settings saved", T.OK)

    def _refresh_key_state(self):
        """The API key only matters for the cloud backend."""
        uses_cloud = config.MODEL_CHOICES[max(0, self.model_combo.current())][1] is None
        self.api_entry.config(state="normal" if uses_cloud else "disabled")
        self.show_key_btn.config(state="normal" if uses_cloud else "disabled")

    def _toggle_key(self):
        self._key_visible = not self._key_visible
        self.api_entry.config(show="" if self._key_visible else "•")
        self.show_key_btn.config(text="hide" if self._key_visible else "show")

    def _on_mic_gain(self, value):
        self.settings.mic_gain_db = float(value)
        self.mic_gain_label.config(text=f"{float(value):+.1f} dB")

    def _on_sys_gain(self, value):
        self.settings.loop_gain_db = float(value)
        self.sys_gain_label.config(text=f"{float(value):+.1f} dB")

    # ==================================================================
    # Ticker
    # ==================================================================
    def _tick(self):
        if self._shutting_down:
            return
        self.mic_meter.set_level(self.engine.mic_level)
        self.sys_meter.set_level(self.engine.sys_level)
        self.status.tick(METER_INTERVAL_MS / 1000.0)

        if self.recording_started_at is not None:
            elapsed = int(time.monotonic() - self.recording_started_at)
            hours, rest = divmod(elapsed, 3600)
            minutes, seconds = divmod(rest, 60)
            self.timer_label.config(
                text=f"{hours}:{minutes:02d}:{seconds:02d}" if hours
                else f"{minutes:02d}:{seconds:02d}")

        self._meter_after_id = self.root.after(METER_INTERVAL_MS, self._tick)

    # ==================================================================
    # Shutdown
    # ==================================================================
    def on_close(self):
        self._shutting_down = True
        if self._meter_after_id is not None:
            try:
                self.root.after_cancel(self._meter_after_id)
            except Exception:
                pass
        self.bridge.stop()

        if self.live_preview is not None:
            self.live_preview.stop()
        if self.finalizer is not None:
            self.finalizer.cancel()

        try:
            self.engine.stop_streams()
        except Exception:
            pass
        try:
            self.pa.terminate()
        except Exception:
            pass

        self.root.destroy()


# ----------------------------------------------------------------------
_STATUS_COLOURS = {
    "red": T.REC, "orange": T.WARN, "purple": T.ACCENT,
    "green": T.OK, "gray": T.TEXT_MUTE, "grey": T.TEXT_MUTE,
}


def _colour(name):
    return _STATUS_COLOURS.get(name, T.TEXT_MUTE)


def _combo(parent, values):
    return ttk.Combobox(parent, state="readonly", style="Dark.TCombobox",
                        font=T.fonts["body"], values=values)


def _index_of(choices, value):
    for index, (_label, code) in enumerate(choices):
        if code == value:
            return index
    return 0
