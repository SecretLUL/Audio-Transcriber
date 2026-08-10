import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import pyaudiowpatch as pyaudio
import soundfile as sf
import numpy as np
import scipy.signal as sps
import math
import re
import threading
import queue
import os
import urllib.request
import zipfile
import io
import subprocess
import time
import json

class DAWMeterCanvas(tk.Canvas):
    def __init__(self, parent, width=340, height=18, **kwargs):
        super().__init__(parent, width=width, height=height, bg="#181818", highlightthickness=1, highlightbackground="#333333", **kwargs)
        self.width = width
        self.height = height
        self.val_pct = 0.0
        self.peak_pct = 0.0
        self.db_val = -60.0
        self.draw_meter(0.0, -60.0)

    def set_level(self, rms_val, gain_db=0.0):
        gain_factor = 10.0 ** (gain_db / 20.0)
        eff_rms = rms_val * gain_factor
        
        if eff_rms < 1e-5:
            db = -60.0
            pct = 0.0
        else:
            db = 20.0 * math.log10(eff_rms)
            pct = min(100.0, max(0.0, ((db + 60.0) / 66.0) * 100.0))
            
        self.val_pct = max(0.0, self.val_pct - 4.5)
        if pct > self.val_pct:
            self.val_pct = pct
            
        if pct > self.peak_pct:
            self.peak_pct = pct
        else:
            self.peak_pct = max(0.0, self.peak_pct - 1.2)

        self.db_val = db
        self.draw_meter(self.val_pct, db)

    def draw_meter(self, pct, db):
        self.delete("all")
        w = self.width
        h = self.height
        fill_w = int(w * (pct / 100.0))

        num_blocks = 32
        block_w = w / num_blocks
        
        for i in range(num_blocks):
            x0 = int(i * block_w + 1)
            x1 = int((i + 1) * block_w - 1)
            block_pct = ((i + 1) / num_blocks) * 100.0
            
            if block_pct <= 65:
                active_color = "#00e676"  # Grün
                dim_color = "#0b3318"
            elif block_pct <= 85:
                active_color = "#ffea00"  # Gelb
                dim_color = "#333004"
            else:
                active_color = "#ff1744"  # Rot
                dim_color = "#3a050c"
                
            color = active_color if fill_w >= x0 else dim_color
            self.create_rectangle(x0, 2, x1, h - 2, fill=color, outline="")

        if self.peak_pct > 2:
            peak_x = int(w * (self.peak_pct / 100.0))
            peak_color = "#ff1744" if self.peak_pct > 85 else "#ffffff"
            self.create_line(peak_x, 1, peak_x, h - 1, fill=peak_color, width=2)

class MegaAudioKiRecorder:
    def __init__(self, root):
        self.root = root
        self.root.title("Lokal Audio KI-Recorder (Mic + System + Whisper + ElevenLabs)")
        self.root.geometry("620x860")  
        self.root.resizable(False, False)
        
        # Audio-Setup
        self.pa = pyaudio.PyAudio()
        self.mic_queue = queue.Queue()
        self.loop_queue = queue.Queue()
        self.is_recording = False
        
        # Kanal & Samplerate-Speicher
        self.mic_channels = 1
        self.loop_channels = 2
        self.mic_rate = 48000
        self.loop_rate = 48000
        
        # Live-Pegel & Gain Variablen (in dB)
        self.raw_mic_rms = 0.0
        self.raw_loop_rms = 0.0
        self.mic_gain_db = 0.0
        self.loop_gain_db = 0.0
        
        # Monitoring & Live-Transkription Steuerung
        self.monitoring_active = False
        self.live_thread_active = False
        self.live_mic_chunks = []
        self.live_loop_chunks = []
        self.last_live_text = ""
        self.monitor_lock = threading.Lock()
        
        self.init_ki_hardware()
        self.setup_ui()
        self.load_audio_devices()
        self.update_meters()

    def init_ki_hardware(self):
        """Erkennt die Hardware für das Whisper-Modell"""
        self.hardware_string = "CPU / Vulkan via whisper.cpp + ElevenLabs API"

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        title_label = ttk.Label(main_frame, text="Audio KI-Recorder & Transkribierer", font=("Arial", 16, "bold"))
        title_label.pack(pady=(0, 5))
        
        hw_label = ttk.Label(main_frame, text=f"KI-Beschleunigung: {self.hardware_string}", font=("Arial", 9, "italic"), foreground="blue")
        hw_label.pack(pady=(0, 15))
        
        # Section 1: Mikrofon
        ttk.Label(main_frame, text="1. Dein Mikrofon (Deine Stimme):", font=("Arial", 10, "bold")).pack(anchor=tk.W, pady=(5, 2))
        self.mic_dropdown = ttk.Combobox(main_frame, width=81, state="readonly")
        self.mic_dropdown.pack(pady=(0, 5))
        self.mic_dropdown.bind("<<ComboboxSelected>>", lambda e: self.restart_monitoring())
        
        # DAW Meter Mic
        mic_meter_frame = ttk.Frame(main_frame)
        mic_meter_frame.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(mic_meter_frame, text="🎤 Mic Pegel:", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.mic_meter = DAWMeterCanvas(mic_meter_frame, width=370, height=18)
        self.mic_meter.grid(row=0, column=1, padx=(0, 5))
        self.mic_db_label = ttk.Label(mic_meter_frame, text="-OFF-", font=("Consolas", 9, "bold"), foreground="#2e7d32", width=9)
        self.mic_db_label.grid(row=0, column=2, sticky=tk.W)
        
        # Gain Slider Mic
        mic_gain_frame = ttk.Frame(main_frame)
        mic_gain_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(mic_gain_frame, text="🎚️ Mic Gain:", font=("Arial", 9, "italic")).grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.mic_gain_slider = ttk.Scale(mic_gain_frame, from_=-20.0, to=20.0, value=0.0, command=self.on_mic_gain_change, length=370)
        self.mic_gain_slider.grid(row=0, column=1, padx=(0, 5))
        self.mic_gain_val_label = ttk.Label(mic_gain_frame, text="+0.0 dB", font=("Consolas", 9, "bold"), width=8)
        self.mic_gain_val_label.grid(row=0, column=2, sticky=tk.W)
        
        # Section 2: System-Sound
        ttk.Label(main_frame, text="2. Dein Headset / Boxen (Stimmen der anderen):", font=("Arial", 10, "bold")).pack(anchor=tk.W, pady=(5, 2))
        self.loop_dropdown = ttk.Combobox(main_frame, width=81, state="readonly")
        self.loop_dropdown.pack(pady=(0, 5))
        self.loop_dropdown.bind("<<ComboboxSelected>>", lambda e: self.restart_monitoring())
        
        # DAW Meter System
        loop_meter_frame = ttk.Frame(main_frame)
        loop_meter_frame.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(loop_meter_frame, text="🔊 Sys Pegel:", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.loop_meter = DAWMeterCanvas(loop_meter_frame, width=370, height=18)
        self.loop_meter.grid(row=0, column=1, padx=(0, 5))
        self.loop_db_label = ttk.Label(loop_meter_frame, text="-OFF-", font=("Consolas", 9, "bold"), foreground="#2e7d32", width=9)
        self.loop_db_label.grid(row=0, column=2, sticky=tk.W)
        
        # Gain Slider System
        loop_gain_frame = ttk.Frame(main_frame)
        loop_gain_frame.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(loop_gain_frame, text="🎚️ Sys Gain:", font=("Arial", 9, "italic")).grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.loop_gain_slider = ttk.Scale(loop_gain_frame, from_=-20.0, to=20.0, value=0.0, command=self.on_loop_gain_change, length=370)
        self.loop_gain_slider.grid(row=0, column=1, padx=(0, 5))
        self.loop_gain_val_label = ttk.Label(loop_gain_frame, text="+0.0 dB", font=("Consolas", 9, "bold"), width=8)
        self.loop_gain_val_label.grid(row=0, column=2, sticky=tk.W)
        
        # Section 3: KI-Modell Auswahl
        ttk.Label(main_frame, text="3. KI-Modell / Service Auswahl:", font=("Arial", 10, "bold")).pack(anchor=tk.W, pady=(5, 2))
        self.model_dropdown = ttk.Combobox(main_frame, width=81, state="readonly")
        self.model_dropdown['values'] = [
            "✨ ElevenLabs Scribe v2 (Cloud API - SOTA Präzision)",
            "tiny (75 MB - Sehr schnell)",
            "base (142 MB - Schnell)",
            "small (466 MB - Ausgewogen)",
            "medium (1.5 GB - Langsam)",
            "large-v3-turbo (1.5 GB - Sehr schnell & hohe Qualität)",
            "large-v3 (3.1 GB - Langsam, maximale Präzision)"
        ]
        self.model_dropdown.current(0)  # Default auf ElevenLabs Scribe v2
        self.model_dropdown.pack(pady=(0, 6))
        
        # Section 4: ElevenLabs API Key
        ttk.Label(main_frame, text="4. ElevenLabs API-Key (Erforderlich für Scribe v2):", font=("Arial", 10, "bold")).pack(anchor=tk.W, pady=(2, 2))
        self.api_key_entry = ttk.Entry(main_frame, width=84, show="*")
        self.api_key_entry.pack(pady=(0, 8))
        
        # Checkbox: Live-Transkription
        self.live_transcribe_var = tk.BooleanVar(value=True)
        self.live_check = ttk.Checkbutton(main_frame, text="⚡ Live-Transkription während der Aufnahme anzeigen", variable=self.live_transcribe_var)
        self.live_check.pack(anchor=tk.W, pady=(0, 8))
        
        # Eingabe: Dateiname (.wav wird erzwungen)
        ttk.Label(main_frame, text="Dateiname für Aufnahme & Protokoll (.wav wird erzwungen):", font=("Arial", 10)).pack(anchor=tk.W)
        self.filename_entry = ttk.Entry(main_frame, width=84)
        self.filename_entry.insert(0, "mein_meeting.wav")
        self.filename_entry.pack(pady=(2, 12))
        
        # Buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack()
        
        self.start_btn = ttk.Button(btn_frame, text="🔴 Starten", command=self.start_recording_thread, width=15)
        self.start_btn.grid(row=0, column=0, padx=4)
        
        self.stop_btn = ttk.Button(btn_frame, text="⬜ Stoppen & KI", command=self.stop_recording, state=tk.DISABLED, width=17)
        self.stop_btn.grid(row=0, column=1, padx=4)

        self.save_btn = ttk.Button(btn_frame, text="💾 Settings speichern", command=self.save_settings, width=19)
        self.save_btn.grid(row=0, column=2, padx=4)

        # Status-Anzeige
        self.status_label = ttk.Label(main_frame, text="Status: Bereit (Pegel aktiv)", font=("Arial", 11, "bold"), foreground="gray")
        self.status_label.pack(pady=(10, 5))

        # Textfenster für das Protokoll
        ttk.Label(main_frame, text="Generiertes KI-Meeting-Protokoll:", font=("Arial", 10, "bold")).pack(anchor=tk.W, pady=(5, 2))
        self.transcript_box = scrolledtext.ScrolledText(main_frame, width=76, height=8, font=("Consolas", 10), state=tk.DISABLED, bg="#fcfcfc")
        self.transcript_box.pack(pady=(0, 5))

    def save_settings(self):
        settings = {
            "mic_device": self.mic_dropdown.get(),
            "loop_device": self.loop_dropdown.get(),
            "mic_gain_db": round(self.mic_gain_db, 1),
            "loop_gain_db": round(self.loop_gain_db, 1),
            "model_index": self.model_dropdown.current(),
            "elevenlabs_api_key": self.api_key_entry.get().strip(),
            "live_transcribe": self.live_transcribe_var.get(),
            "filename": self.filename_entry.get().strip()
        }
        
        cfg_path = os.path.join(os.getcwd(), "settings.json")
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=4, ensure_ascii=False)
            messagebox.showinfo("Erfolg", "Deine Einstellungen wurden erfolgreich in 'settings.json' gespeichert!")
        except Exception as e:
            messagebox.showerror("Fehler", f"Einstellungen konnten nicht gespeichert werden:\n{e}")

    def load_settings(self):
        cfg_path = os.path.join(os.getcwd(), "settings.json")
        if not os.path.exists(cfg_path):
            return
            
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
                
            if "mic_device" in settings and settings["mic_device"] in self.mic_dropdown['values']:
                self.mic_dropdown.set(settings["mic_device"])
            if "loop_device" in settings and settings["loop_device"] in self.loop_dropdown['values']:
                self.loop_dropdown.set(settings["loop_device"])
                
            if "mic_gain_db" in settings:
                m_g = float(settings["mic_gain_db"])
                self.mic_gain_slider.set(m_g)
                self.on_mic_gain_change(m_g)
                
            if "loop_gain_db" in settings:
                l_g = float(settings["loop_gain_db"])
                self.loop_gain_slider.set(l_g)
                self.on_loop_gain_change(l_g)
                
            if "model_index" in settings and 0 <= settings["model_index"] < len(self.model_dropdown['values']):
                self.model_dropdown.current(settings["model_index"])
                
            if "elevenlabs_api_key" in settings:
                self.api_key_entry.delete(0, tk.END)
                self.api_key_entry.insert(0, settings["elevenlabs_api_key"])
                
            if "live_transcribe" in settings:
                self.live_transcribe_var.set(bool(settings["live_transcribe"]))
                
            if "filename" in settings and settings["filename"]:
                self.filename_entry.delete(0, tk.END)
                self.filename_entry.insert(0, settings["filename"])
                
        except Exception:
            pass

    def on_mic_gain_change(self, val):
        v = float(val)
        self.mic_gain_db = v
        self.mic_gain_val_label.config(text=f"{v:+.1f} dB")

    def on_loop_gain_change(self, val):
        v = float(val)
        self.loop_gain_db = v
        self.loop_gain_val_label.config(text=f"{v:+.1f} dB")

    def load_audio_devices(self):
        mic_options = []
        loop_options = []
        mix_keywords = ['loopback', 'stereomix', 'stereo mix', 'was sie hören', 'mischpult', 'cable']

        for i in range(self.pa.get_device_count()):
            try: dev_info = self.pa.get_device_info_by_index(i)
            except Exception: continue
                
            display_name = f"{i}: {dev_info['name']}"
            name_lower = dev_info['name'].lower()
            
            if dev_info["maxInputChannels"] > 0 and not dev_info.get("isLoopbackDevice") and not any(kw in name_lower for kw in ['loopback', 'stereomix']):
                mic_options.append(display_name)
            if dev_info["maxOutputChannels"] > 0 or dev_info.get("isLoopbackDevice") or any(kw in name_lower for kw in mix_keywords):
                loop_options.append(display_name)

        self.mic_dropdown['values'] = mic_options
        self.loop_dropdown['values'] = loop_options

        if mic_options:
            yeti_idx = [idx for idx, s in enumerate(mic_options) if "Yeti" in s]
            self.mic_dropdown.current(yeti_idx[0] if yeti_idx else 0)
        if loop_options:
            loop_fav = [idx for idx, s in enumerate(loop_options) if "Loopback" in s or "PRO X 2" in s]
            self.loop_dropdown.current(loop_fav[0] if loop_fav else 0)
            
        self.load_settings()
        self.restart_monitoring()

    def restart_monitoring(self):
        self.monitoring_active = False
        time.sleep(0.05)
        
        if not self.mic_dropdown.get() or not self.loop_dropdown.get():
            return
            
        mic_id = int(self.mic_dropdown.get().split(":")[0])
        selected_loop_id = int(self.loop_dropdown.get().split(":")[0])
        
        dev_info = self.pa.get_device_info_by_index(selected_loop_id)
        loop_id = selected_loop_id
        
        if not dev_info.get("isLoopbackDevice") and dev_info["maxOutputChannels"] > 0:
            target_name = dev_info["name"].strip()
            clean_target = target_name[:15].lower()
            found = False
            
            for i in range(self.pa.get_device_count()):
                chk = self.pa.get_device_info_by_index(i)
                if chk["maxInputChannels"] > 0 and chk.get("isLoopbackDevice"):
                    if clean_target in chk["name"].lower():
                        loop_id = i; found = True; break
            if not found:
                for i in range(self.pa.get_device_count()):
                    chk = self.pa.get_device_info_by_index(i)
                    if chk["maxInputChannels"] > 0 and chk.get("isLoopbackDevice"):
                        loop_id = i; found = True; break
        
        self.monitoring_active = True
        threading.Thread(target=self.monitor_stream, args=(mic_id, True), daemon=True).start()
        threading.Thread(target=self.monitor_stream, args=(loop_id, False), daemon=True).start()

    def monitor_stream(self, device_id, is_mic):
        p = pyaudio.PyAudio()
        try:
            dev_info = p.get_device_info_by_index(device_id)
            channels = max(1, dev_info["maxInputChannels"])
            rate = int(dev_info["defaultSampleRate"])
            
            stream = p.open(format=pyaudio.paFloat32, channels=channels, rate=rate, input=True, input_device_index=device_id, frames_per_buffer=1024)
            while self.monitoring_active:
                data = stream.read(1024, exception_on_overflow=False)
                if not data: continue
                audio_chunk = np.frombuffer(data, dtype=np.float32)
                
                if len(audio_chunk) > 0:
                    rms = np.sqrt(np.mean(audio_chunk**2))
                    if is_mic: self.raw_mic_rms = rms
                    else: self.raw_loop_rms = rms
                
                if self.is_recording:
                    if is_mic:
                        self.mic_queue.put(audio_chunk)
                        self.live_mic_chunks.append(audio_chunk)
                        self.mic_channels = channels
                        self.mic_rate = rate
                    else:
                        self.loop_queue.put(audio_chunk)
                        self.live_loop_chunks.append(audio_chunk)
                        self.loop_channels = channels
                        self.loop_rate = rate
            stream.stop_stream(); stream.close()
        except Exception:
            pass
        finally: p.terminate()

    def update_meters(self):
        self.mic_meter.set_level(self.raw_mic_rms, self.mic_gain_db)
        self.loop_meter.set_level(self.raw_loop_rms, self.loop_gain_db)
        
        if self.mic_meter.db_val > -59.0:
            self.mic_db_label.config(text=f"{self.mic_meter.db_val:+.1f} dB")
        else:
            self.mic_db_label.config(text="-OFF-")
            
        if self.loop_meter.db_val > -59.0:
            self.loop_db_label.config(text=f"{self.loop_meter.db_val:+.1f} dB")
        else:
            self.loop_db_label.config(text="-OFF-")

        self.root.after(35, self.update_meters)

    def start_recording_thread(self):
        if not self.mic_dropdown.get() or not self.loop_dropdown.get():
            messagebox.showwarning("Warnung", "Bitte wähle Ton-Eingang und Ton-Ausgang aus.")
            return
        self.start_recording()

    def start_recording(self):
        self.mic_queue = queue.Queue()
        self.loop_queue = queue.Queue()
        self.live_mic_chunks = []
        self.live_loop_chunks = []
        self.last_live_text = ""
        self.is_recording = True
        
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.mic_dropdown.config(state=tk.DISABLED)
        self.loop_dropdown.config(state=tk.DISABLED)
        self.status_label.config(text="● NIMMT AUF...", foreground="red")

        if self.live_transcribe_var.get():
            self.live_thread_active = True
            threading.Thread(target=self.run_live_transcription_loop, daemon=True).start()

    def determine_speaker(self, m_scaled, l_scaled, t_start, t_end, sr=16000):
        if m_scaled is None and l_scaled is None:
            return ""
        if m_scaled is None or len(m_scaled) == 0:
            return "[Desktop/Teilnehmer]: "
        if l_scaled is None or len(l_scaled) == 0:
            return "[Du]: "

        i_start = max(0, int(t_start * sr))
        i_end = min(max(len(m_scaled), len(l_scaled)), int(max(t_end, t_start + 0.4) * sr))
        
        if i_start >= i_end:
            return "[Du]: "

        m_part = m_scaled[i_start:i_end] if i_start < len(m_scaled) else np.array([])
        l_part = l_scaled[i_start:i_end] if i_start < len(l_scaled) else np.array([])

        m_rms = float(np.sqrt(np.mean(m_part**2))) if len(m_part) > 0 else 0.0
        l_rms = float(np.sqrt(np.mean(l_part**2))) if len(l_part) > 0 else 0.0

        # Wenn weder auf dem Mikrofon noch auf dem Desktop Ton vorliegt -> Stille (Whisper-Haluzination abfangen!)
        if m_rms < 0.0025 and l_rms < 0.0025:
            return None

        m_valid = m_rms > 0.0025
        l_valid = l_rms > 0.0025

        if m_valid and not l_valid:
            return "[Du]: "
        elif l_valid and not m_valid:
            return "[Desktop/Teilnehmer]: "
        elif m_valid and l_valid:
            return "[Du]: " if m_rms >= l_rms * 1.05 else "[Desktop/Teilnehmer]: "
        else:
            return "[Du]: " if m_rms >= l_rms else "[Desktop/Teilnehmer]: "

    def run_live_transcription_loop(self):
        time.sleep(2.5)
        bin_dir = os.path.join(os.getcwd(), "bin")
        exe_path = os.path.join(bin_dir, "whisper-cli.exe")
        
        model_selection = self.model_dropdown.get().split(" ")[0].strip()
        if "ElevenLabs" in model_selection or "✨" in model_selection:
            model_selection = "small"
            
        model_filename = f"ggml-{model_selection}.bin"
        model_path = os.path.join(bin_dir, model_filename)
        
        if not os.path.exists(exe_path) or not os.path.exists(model_path):
            # Falls small nicht da ist, versuche tiny oder base
            for alt in ["tiny", "base", "large-v3-turbo"]:
                alt_path = os.path.join(bin_dir, f"ggml-{alt}.bin")
                if os.path.exists(alt_path):
                    model_path = alt_path
                    break
            else:
                return
            
        out_dir = os.path.join(os.getcwd(), "output")
        os.makedirs(out_dir, exist_ok=True)
        temp_live_wav = os.path.join(out_dir, ".temp_live.wav")
        
        while self.is_recording and self.live_thread_active:
            m_chunks = list(self.live_mic_chunks)
            l_chunks = list(self.live_loop_chunks)
            
            mic_16k = self.resample_to_16k_mono(m_chunks, self.mic_channels, self.mic_rate)
            loop_16k = self.resample_to_16k_mono(l_chunks, self.loop_channels, self.loop_rate)
            
            if mic_16k is not None or loop_16k is not None:
                m_gain = 10.0 ** (self.mic_gain_db / 20.0)
                l_gain = 10.0 ** (self.loop_gain_db / 20.0)
                
                m_scaled = (mic_16k * m_gain) if mic_16k is not None else None
                l_scaled = (loop_16k * l_gain) if loop_16k is not None else None
                
                if m_scaled is not None and np.max(np.abs(m_scaled)) > 0.95:
                    m_scaled = m_scaled * (0.95 / np.max(np.abs(m_scaled)))
                if l_scaled is not None and np.max(np.abs(l_scaled)) > 0.95:
                    l_scaled = l_scaled * (0.95 / np.max(np.abs(l_scaled)))
                
                if m_scaled is not None and l_scaled is not None:
                    min_len = min(len(m_scaled), len(l_scaled))
                    stereo_audio = np.column_stack([m_scaled[:min_len], l_scaled[:min_len]])
                elif m_scaled is not None:
                    stereo_audio = np.column_stack([m_scaled, np.zeros_like(m_scaled)])
                else:
                    stereo_audio = np.column_stack([np.zeros_like(l_scaled), l_scaled])
                
                try:
                    sf.write(temp_live_wav, stereo_audio, 16000, subtype='PCM_16')
                    
                    cmd = [
                        exe_path,
                        "-m", model_path,
                        "-f", temp_live_wav,
                        "-l", "de",
                        "-ng",
                        "-ml", "45",
                        "-sow"
                    ]
                    
                    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore", creationflags=creationflags)
                    
                    live_text = ""
                    last_seen_txt = ""
                    consecutive_repeats = 0
                    for line in proc.stdout:
                        clean = line.strip()
                        match = re.match(r'^\s*\[(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*-->\s*(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\]\s*(.*)$', clean)
                        if match:
                            s_p1, s_p2, s_p3 = int(match.group(1)), int(match.group(2)), int(match.group(3)) if match.group(3) else None
                            e_p1, e_p2, e_p3 = int(match.group(4)), int(match.group(5)), int(match.group(6)) if match.group(6) else None
                            txt = match.group(7).strip()
                            
                            t_start = (s_p1 * 3600 + s_p2 * 60 + s_p3) if s_p3 is not None else (s_p1 * 60 + s_p2)
                            t_end = (e_p1 * 3600 + e_p2 * 60 + e_p3) if e_p3 is not None else (e_p1 * 60 + e_p2)
                            
                            spk = self.determine_speaker(m_scaled, l_scaled, t_start, t_end)
                            if spk is None:
                                continue # Stille: Whisper-Haluzination abfangen!
                                
                            if txt == last_seen_txt:
                                consecutive_repeats += 1
                                if consecutive_repeats >= 2:
                                    continue # Endlosschleife / Haluzinations-Wiederholung abfangen!
                            else:
                                last_seen_txt = txt
                                consecutive_repeats = 1
                                
                            live_text += f"[{t_start//60:02d}:{t_start%60:02d}] {spk}{txt}\n"
                    proc.wait()
                    
                    if live_text.strip():
                        self.last_live_text = live_text
                        def update_gui_live(t=live_text):
                            if self.is_recording:
                                self.transcript_box.config(state=tk.NORMAL)
                                self.transcript_box.delete("1.0", tk.END)
                                self.transcript_box.insert(tk.END, "⚡ [LIVE-TRANSKRIPTION AKTIV]:\n" + t)
                                self.transcript_box.see(tk.END)
                                self.transcript_box.config(state=tk.DISABLED)
                        self.root.after(0, update_gui_live)
                except Exception:
                    pass
            
            for _ in range(30):
                if not self.is_recording or not self.live_thread_active: break
                time.sleep(0.1)

    def resample_to_16k_mono(self, audio_chunks, channels, orig_rate):
        if not audio_chunks:
            return None
        raw_data = np.concatenate(audio_chunks)
        
        # Sicheres Reshaping (vollständige Samples erzwingen)
        valid_samples = (len(raw_data) // channels) * channels
        if valid_samples == 0:
            return None
        
        arr = raw_data[:valid_samples]
        if channels > 1:
            reshaped = arr.reshape(-1, channels)
            ch_rms = np.sqrt(np.mean(reshaped**2, axis=0))
            max_rms = np.max(ch_rms)
            if max_rms > 1e-5:
                # Nur aktive Kanäle mitteln (verhindert Lautstärkeverlust durch stumme Surround-Kanäle 3..8)
                active_mask = ch_rms > (max_rms * 0.1)
                mono = reshaped[:, active_mask].mean(axis=1)
            else:
                mono = reshaped.mean(axis=1)
        else:
            mono = arr
            
        if orig_rate == 16000:
            return mono
            
        # Exakte Polyphasen-Resampling auf 16 kHz
        gcd = math.gcd(16000, orig_rate)
        up = 16000 // gcd
        down = orig_rate // gcd
        resampled = sps.resample_poly(mono, up, down)
        return resampled

    def stop_recording(self):
        self.is_recording = False
        self.live_thread_active = False
        self.status_label.config(text="Verarbeite und speichere Audiodaten...", foreground="orange")
        self.root.update()
        self.root.after(600)  
        
        mic_chunks = []
        while not self.mic_queue.empty(): mic_chunks.append(self.mic_queue.get())
        loop_chunks = []
        while not self.loop_queue.empty(): loop_chunks.append(self.loop_queue.get())
        
        mic_mono_16k = self.resample_to_16k_mono(mic_chunks, self.mic_channels, self.mic_rate)
        loop_mono_16k = self.resample_to_16k_mono(loop_chunks, self.loop_channels, self.loop_rate)
        
        if mic_mono_16k is not None or loop_mono_16k is not None:
            m_gain = 10.0 ** (self.mic_gain_db / 20.0)
            l_gain = 10.0 ** (self.loop_gain_db / 20.0)
            
            m_scaled = (mic_mono_16k * m_gain) if mic_mono_16k is not None else None
            l_scaled = (loop_mono_16k * l_gain) if loop_mono_16k is not None else None
            
            if m_scaled is not None and np.max(np.abs(m_scaled)) > 0.95:
                m_scaled = m_scaled * (0.95 / np.max(np.abs(m_scaled)))
            if l_scaled is not None and np.max(np.abs(l_scaled)) > 0.95:
                l_scaled = l_scaled * (0.95 / np.max(np.abs(l_scaled)))
            
            # 2-Kanal Audio für exakte Sprecher-Erkennung (Kanal 0 = Mic / Du, Kanal 1 = Desktop / Teilnehmer)
            if m_scaled is not None and l_scaled is not None:
                min_len = min(len(m_scaled), len(l_scaled))
                stereo_audio = np.column_stack([m_scaled[:min_len], l_scaled[:min_len]])
            elif m_scaled is not None:
                stereo_audio = np.column_stack([m_scaled, np.zeros_like(m_scaled)])
            else:
                stereo_audio = np.column_stack([np.zeros_like(l_scaled), l_scaled])
            
            out_dir = os.path.join(os.getcwd(), "output")
            os.makedirs(out_dir, exist_ok=True)
            
            input_name = self.filename_entry.get().strip()
            if not input_name: input_name = "mein_meeting.wav"
            clean_name = os.path.basename(os.path.splitext(input_name)[0]) + ".wav"
            output_file = os.path.join(out_dir, clean_name)
            
            try:
                # Als saubere 16 kHz Stereo PCM_16 WAV speichern
                sf.write(output_file, stereo_audio, 16000, subtype='PCM_16')
                
                # Falls Live-Transkription an war UND wir lokal transkribieren, ist das Transkript sofort fertig!
                model_sel = self.model_dropdown.get()
                if self.live_transcribe_var.get() and self.last_live_text.strip() and "ElevenLabs" not in model_sel:
                    txt_path = os.path.join(out_dir, os.path.splitext(clean_name)[0] + ".txt")
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(self.last_live_text)
                    self.transcription_success(self.last_live_text, txt_path)
                else:
                    self.start_ki_transcription_thread(output_file)
            except Exception as e:
                messagebox.showerror("Fehler", f"Datei-Schreibfehler:\n{e}")
                self.reset_ui()
        else:
            messagebox.showwarning("Keine Daten", "Es wurden keine Audiodaten erfasst.")
            self.reset_ui()

    def write_to_log(self, message):
        def append():
            self.transcript_box.config(state=tk.NORMAL)
            self.transcript_box.insert(tk.END, message)
            self.transcript_box.see(tk.END)
            self.transcript_box.config(state=tk.DISABLED)
        self.root.after(0, append)

    def download_with_progress(self, url, dest_path, description):
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            block_size = 1024 * 1024 # 1 MB chunks
            
            self.write_to_log(f"\nStarte Download von {description}...\n")
            
            with open(dest_path, 'wb') as f:
                start_time = time.time()
                last_update_time = start_time
                
                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    downloaded += len(buffer)
                    f.write(buffer)
                    
                    current_time = time.time()
                    if current_time - last_update_time > 0.5 or downloaded == total_size:
                        last_update_time = current_time
                        elapsed = current_time - start_time
                        speed = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                        pct = (downloaded / total_size) * 100 if total_size > 0 else 0
                        mb_downloaded = downloaded / (1024 * 1024)
                        mb_total = total_size / (1024 * 1024)
                        
                        progress_text = f"\rLade: {pct:5.1f}% ({mb_downloaded:.1f}/{mb_total:.1f} MB) bei {speed:.1f} MB/s"
                        
                        def update_progress(msg=progress_text):
                            self.transcript_box.config(state=tk.NORMAL)
                            last_line_idx = self.transcript_box.index("end-1c linestart")
                            if last_line_idx != "1.0":
                                self.transcript_box.delete(last_line_idx, tk.END)
                                self.transcript_box.insert(tk.END, msg)
                            else:
                                self.transcript_box.insert(tk.END, msg)
                            self.transcript_box.see(tk.END)
                            self.transcript_box.config(state=tk.DISABLED)
                        self.root.after(0, update_progress)
            
            self.write_to_log(f"\nDownload abgeschlossen: {description}\n")

    def start_ki_transcription_thread(self, audio_path):
        self.stop_btn.config(state=tk.DISABLED)
        model_sel = self.model_dropdown.get()
        
        if "ElevenLabs" in model_sel:
            api_key = self.api_key_entry.get().strip()
            if not api_key:
                messagebox.showerror("API-Key fehlt", "Bitte trage deinen ElevenLabs API-Key ein, um Scribe v2 zu nutzen.")
                self.reset_ui()
                return
            self.status_label.config(text="✨ TRANSKRIBIERT VIA ELEVENLABS (Scribe v2)...", foreground="purple")
            self.transcript_box.config(state=tk.NORMAL)
            self.transcript_box.delete("1.0", tk.END)
            self.transcript_box.insert(tk.END, "Sende Audiodatei an ElevenLabs Scribe v2 API...\n")
            self.transcript_box.config(state=tk.DISABLED)
            self.root.update()
            threading.Thread(target=self.run_elevenlabs_transcription, args=(audio_path, api_key), daemon=True).start()
        else:
            self.status_label.config(text="🤖 KI TRANSKRIBIERT (Whisper)... Bitte warten!", foreground="purple")
            self.transcript_box.config(state=tk.NORMAL)
            self.transcript_box.delete("1.0", tk.END)
            self.transcript_box.insert(tk.END, "Initialisiere Whisper-Ressourcen...\n")
            self.transcript_box.config(state=tk.DISABLED)
            self.root.update()
            threading.Thread(target=self.run_whisper_transcription, args=(audio_path,), daemon=True).start()

    def run_elevenlabs_transcription(self, audio_path, api_key):
        try:
            self.write_to_log("Verbinde mit ElevenLabs Scribe v2 API...\n")
            
            url = "https://api.elevenlabs.io/v1/speech-to-text"
            boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
            
            with open(audio_path, "rb") as f:
                file_bytes = f.read()
                
            filename = os.path.basename(audio_path)
            
            body = bytearray()
            
            # model_id parameter
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="model_id"\r\n\r\n'.encode("utf-8"))
            body.extend("scribe_v2\r\n".encode("utf-8"))
            
            # tag_audio_events parameter
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="tag_audio_events"\r\n\r\n'.encode("utf-8"))
            body.extend("true\r\n".encode("utf-8"))
            
            # file parameter
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
            body.extend(b'Content-Type: audio/wav\r\n\r\n')
            body.extend(file_bytes)
            body.extend(b'\r\n')
            
            # end boundary
            body.extend(f"--{boundary}--\r\n".encode("utf-8"))
            
            req = urllib.request.Request(
                url,
                data=bytes(body),
                headers={
                    "xi-api-key": api_key.strip(),
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "Mozilla/5.0"
                },
                method="POST"
            )
            
            with urllib.request.urlopen(req) as resp:
                res_data = resp.read().decode("utf-8")
                result_json = json.loads(res_data)
                
            self.write_to_log("Antwort von ElevenLabs Scribe v2 empfangen!\nAnalysiere Wort-Timestamps & Sprecher-Zuordnung...\n\n")
            
            m_scaled, l_scaled = None, None
            try:
                audio_data, audio_sr = sf.read(audio_path)
                if audio_data.ndim == 2 and audio_data.shape[1] >= 2:
                    m_scaled = audio_data[:, 0]
                    l_scaled = audio_data[:, 1]
            except Exception:
                pass
                
            formatted_lines = []
            
            words = result_json.get("words", [])
            if words:
                current_words = []
                current_start = None
                
                for w_info in words:
                    w_text = w_info.get("text", "")
                    w_start = w_info.get("start", 0.0)
                    w_end = w_info.get("end", 0.0)
                    
                    if current_start is None:
                        current_start = w_start
                        
                    current_words.append(w_text)
                    
                    # Satzende erkennen (Punkt, Fragezeichen, Ausrufezeichen oder Wortanzahl >= 15)
                    is_punct = any(w_text.endswith(p) for p in [".", "?", "!", "\n"])
                    if is_punct or (len(current_words) >= 15):
                        sent_text = " ".join(current_words).strip()
                        spk = self.determine_speaker(m_scaled, l_scaled, current_start, w_end)
                        if spk is None:
                            spk = "[Du]: "
                            
                        mins = int(current_start) // 60
                        secs = int(current_start) % 60
                        line_str = f"[{mins:02d}:{secs:02d}] {spk}{sent_text}"
                        formatted_lines.append(line_str)
                        self.write_to_log(line_str + "\n")
                        
                        current_words = []
                        current_start = None
                        
                if current_words:
                    sent_text = " ".join(current_words).strip()
                    spk = self.determine_speaker(m_scaled, l_scaled, current_start, current_start + 1.0)
                    if spk is None: spk = "[Du]: "
                    mins = int(current_start) // 60
                    secs = int(current_start) % 60
                    line_str = f"[{mins:02d}:{secs:02d}] {spk}{sent_text}"
                    formatted_lines.append(line_str)
                    self.write_to_log(line_str + "\n")
            else:
                full_text = result_json.get("text", "").strip()
                if full_text:
                    line_str = f"[00:00] [Du/Teilnehmer]: {full_text}"
                    formatted_lines.append(line_str)
                    self.write_to_log(line_str + "\n")
                    
            final_text = "\n".join(formatted_lines)
            if not final_text.strip():
                final_text = "[ElevenLabs Scribe v2 hat keinen gesprochenen Text in der Audio erkannt.]"
                
            txt_path = os.path.splitext(audio_path)[0] + ".txt"
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(final_text)
                
            self.root.after(0, lambda: self.transcription_success(final_text, txt_path))
            
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            self.root.after(0, lambda: self.transcription_failed(f"ElevenLabs API Fehler ({e.code}):\n{err_body}"))
        except Exception as e:
            self.root.after(0, lambda: self.transcription_failed(f"ElevenLabs API Anfrage fehlgeschlagen:\n{e}"))

    def run_whisper_transcription(self, audio_path):
        try:
            bin_dir = os.path.join(os.getcwd(), "bin")
            os.makedirs(bin_dir, exist_ok=True)
            
            exe_path = os.path.join(bin_dir, "whisper-cli.exe")
            zip_url = "https://github.com/lemonade-sdk/whisper.cpp-rocm/releases/download/v1.8.4/whisper-v1.8.4-windows-vulkan-x64.zip"
            zip_path = os.path.join(bin_dir, "whisper-vulkan.zip")
            
            # 1. whisper.cpp herunterladen falls nicht vorhanden
            if not os.path.exists(exe_path):
                self.write_to_log("Lade whisper.cpp Binärdateien herunter...\n")
                self.download_with_progress(zip_url, zip_path, "whisper.cpp")
                
                self.write_to_log("Entpacke Binärdateien...\n")
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(bin_dir)
                
                try: os.remove(zip_path)
                except: pass
                
                # Unnötige Beispiel-Tools aufräumen
                essential = {'whisper-cli.exe', 'whisper.dll', 'ggml.dll', 'ggml-base.dll', 'ggml-cpu.dll', 'ggml-vulkan.dll', 'SDL2.dll'}
                for f_item in os.listdir(bin_dir):
                    if f_item.endswith('.exe') and f_item not in essential:
                        try: os.remove(os.path.join(bin_dir, f_item))
                        except: pass
                        
                self.write_to_log("whisper.cpp erfolgreich eingerichtet!\n")
            
            # 2. Modell herunterladen falls nicht vorhanden
            model_selection = self.model_dropdown.get().split(" ")[0].strip()
            model_filename = f"ggml-{model_selection}.bin"
            model_path = os.path.join(bin_dir, model_filename)
            model_url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{model_filename}"
            
            if not os.path.exists(model_path):
                self.write_to_log(f"Lade Whisper-Modell '{model_selection}' herunter...\n")
                self.download_with_progress(model_url, model_path, f"Whisper-Modell ({model_selection})")
            
            # 3. Transkription starten via whisper-cli.exe
            self.write_to_log("\nStarte KI-Transkription via Whisper...\n")
            
            cmd = [
                exe_path,
                "-m", model_path,
                "-f", audio_path,
                "-l", "de",
                "-ng",
                "-ml", "45",
                "-sow"
            ]
            
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=creationflags
            )
            
            stderr_output = []
            def read_stderr(pipe):
                for line in pipe:
                    clean_line = line.strip()
                    if clean_line:
                        stderr_output.append(clean_line)
                        self.write_to_log(f"[Whisper System] {clean_line}\n")
            
            stderr_thread = threading.Thread(target=read_stderr, args=(process.stderr,), daemon=True)
            stderr_thread.start()
            
            # 0. Audio-Datei laden für 100% physikalisch exakte Spuranalyse pro Satz
            m_scaled, l_scaled = None, None
            try:
                audio_data, audio_sr = sf.read(audio_path)
                if audio_data.ndim == 2 and audio_data.shape[1] >= 2:
                    m_scaled = audio_data[:, 0]
                    l_scaled = audio_data[:, 1]
            except Exception:
                pass

            formatted_text = ""
            last_seen_txt = ""
            consecutive_repeats = 0
            for line in process.stdout:
                clean_line = line.strip()
                if not clean_line:
                    continue
                
                # Präzises Parsem von Whisper-Timestamps & physikalischer Sprecher-Bestimmung
                match = re.match(r'^\s*\[(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*-->\s*(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\]\s*(.*)$', clean_line)
                if match:
                    s_p1, s_p2, s_p3 = int(match.group(1)), int(match.group(2)), int(match.group(3)) if match.group(3) else None
                    e_p1, e_p2, e_p3 = int(match.group(4)), int(match.group(5)), int(match.group(6)) if match.group(6) else None
                    text_part = match.group(7).strip()
                    
                    t_start = (s_p1 * 3600 + s_p2 * 60 + s_p3) if s_p3 is not None else (s_p1 * 60 + s_p2)
                    t_end = (e_p1 * 3600 + e_p2 * 60 + e_p3) if e_p3 is not None else (e_p1 * 60 + e_p2)
                    
                    speaker_label = self.determine_speaker(m_scaled, l_scaled, t_start, t_end)
                    if speaker_label is None:
                        continue # Stille: Whisper-Haluzination abfangen!
                        
                    if text_part == last_seen_txt:
                        consecutive_repeats += 1
                        if consecutive_repeats >= 2:
                            continue # Endlosschleife / Haluzinations-Wiederholung abfangen!
                    else:
                        last_seen_txt = text_part
                        consecutive_repeats = 1
                        
                    mins = t_start // 60
                    secs = t_start % 60
                    formatted_line = f"[{mins:02d}:{secs:02d}] {speaker_label}{text_part}"
                else:
                    formatted_line = clean_line
                
                formatted_text += formatted_line + "\n"
                self.write_to_log(formatted_line + "\n")
            
            process.wait()
            
            if process.returncode != 0:
                err_msg = "\n".join(stderr_output[-10:]) if stderr_output else f"Exit code {process.returncode}"
                raise RuntimeError(f"Whisper-Prozess fehlgeschlagen ({process.returncode}):\n{err_msg}")
            
            if not formatted_text.strip():
                formatted_text = "[Audio verarbeitet, aber es mehere Tonpausen ohne Sprache gab.]"
            
            txt_path = os.path.splitext(audio_path)[0] + ".txt"
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(formatted_text)
                
            self.root.after(0, lambda: self.transcription_success(formatted_text, txt_path))
            
        except Exception as e:
            self.root.after(0, lambda: self.transcription_failed(str(e)))

    def transcription_success(self, text, txt_path):
        self.transcript_box.config(state=tk.NORMAL)
        self.transcript_box.delete("1.0", tk.END)
        self.transcript_box.insert(tk.END, text)
        self.transcript_box.config(state=tk.DISABLED)
        
        self.status_label.config(text="✔ FERTIG! Protokoll wurde gespeichert.", foreground="green")
        messagebox.showinfo("KI-Erfolg", f"Meeting erfolgreich transkribiert!\n\nAudiodatei: {os.path.basename(txt_path).replace('.txt', '.wav')}\nTextprotokoll: {os.path.basename(txt_path)}")
        self.reset_ui()

    def transcription_failed(self, error_msg):
        self.transcript_box.config(state=tk.NORMAL)
        self.transcript_box.insert(tk.END, f"\n\n[FEHLER WÄHREND DER KI-VERARBEITUNG]:\n{error_msg}")
        self.transcript_box.config(state=tk.DISABLED)
        
        self.status_label.config(text="❌ KI-Fehler aufgetreten.", foreground="red")
        messagebox.showerror("KI-Fehler", f"Die Transkription ist fehlgeschlagen:\n{error_msg}")
        self.reset_ui()

    def reset_ui(self):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.mic_dropdown.config(state="readonly")
        self.loop_dropdown.config(state="readonly")
        self.raw_mic_rms = 0.0
        self.raw_loop_rms = 0.0

if __name__ == "__main__":
    root = tk.Tk()
    app = MegaAudioKiRecorder(root)
    root.mainloop()
