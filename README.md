# 🎙️ Audio AI Recorder & Transcriber 🚀

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%20WASAPI-0078D6.svg)](https://docs.microsoft.com/en-us/windows/win32/coreaudio/wasapi)
[![FOSS: 100%](https://img.shields.io/badge/FOSS-100%25-brightgreen.svg)](#-license--foss)

An advanced, privacy-focused Windows audio recording and AI transcription suite. It captures **microphone** and **system audio** independently and generates high-accuracy meeting transcripts with precise **speaker attribution** — locally via `whisper.cpp` or in the cloud via the ElevenLabs Scribe API.

---

## ✨ Features

- 🎤 **Dual-Track Capture**: Separate recording of your voice (microphone) and other participants (WASAPI loopback system audio).
- 🗣️ **Smart Speaker Diarization**: Exact speaker tagging (`[You]` vs `[Participant]`) via track-relative level comparison.
- 🤖 **Offline Local AI**: Integrated `whisper.cpp` engine with automatic GGML model downloads (Tiny to Large-v3).
- ☁️ **Cloud API Acceleration**: Optional ElevenLabs Scribe v2 integration with token-level timestamp alignment.
- ⚡ **Real-Time Live Preview**: Streaming live transcription while recording without cutting off closing audio.
- 🎛️ **DAW-Grade VUMeters**: Custom hand-drawn peak/RMS meters with dynamic dB readouts and gain sliders (-20 dB to +20 dB).
- 🔒 **Encrypted Secret Storage**: ElevenLabs API keys are encrypted via Windows DPAPI bound to your user account.
- 🎨 **Modern Dark Interface**: Custom Tkinter `Canvas` design system with zero external UI framework dependencies.
- 🆓 **100% Free & Open Source**: Released under the permissive MIT License.

---

## 🚀 Quick Start

### 1. Installation

Install the required Python packages:

```shell
pip install -r requirements.txt
```

> 💡 **Note**: `whisper.cpp` binaries and GGML models are automatically fetched on demand to the `bin/` directory.

### 2. Running the Application

* **Double-click launch** (no console window):
  ```shell
  Start-Recorder.vbs
  ```
* **Command line launch** (with debugging logs):
  ```shell
  python main.py
  ```

---

## 🏗️ Architecture & Project Structure

```text
📁 Audio-Transcriber/
 ├── 📄 main.py                     🚀 Entry point for python / pythonw launch
 ├── 📜 Start-Recorder.vbs          🖱️ Windows double-click launcher
 ├── 📄 requirements.txt            📦 Core dependencies (pyaudiowpatch, soundfile, scipy, numpy)
 ├── 📄 LICENSE                     📄 MIT License (100% FOSS)
 ├── 📄 README.md                   📖 Project documentation
 ├── 📁 audio_transcriber/          📦 Main application package
 │    ├── 📄 paths.py               📂 Central path resolution & filename sanitization
 │    ├── 📄 config.py              ⚙️ Typed & versioned settings (Schema v2)
 │    ├── 📄 secretstore.py         🔐 DPAPI Windows key encryption
 │    ├── 📄 events.py              🌉 Thread-safe UI event bridge & pump
 │    ├── 📄 diarize.py             🗣️ Speaker merging, bleed & hallucination filter
 │    ├── 📄 pipeline.py            🔄 Post-processing workflow & live preview engine
 │    ├── 📁 audio/                 🔊 Audio processing module
 │    │    ├── 📄 devices.py        🔌 WASAPI enumeration & loopback matching
 │    │    ├── 📄 capture.py        🎙️ Multi-device audio capture & disk streaming
 │    │    └── 📄 dsp.py            🎛️ Active channel downmix & 16 kHz polyphase resampling
 │    ├── 📁 transcribe/            🤖 AI Transcription engines
 │    │    ├── 📄 base.py           🧩 Shared backend interface & timestamp parsers
 │    │    ├── 📄 binaries.py       📥 Atomic model & binary downloader
 │    │    ├── 📄 whispercpp.py     💻 Local whisper.cpp CLI subprocess runner
 │    │    └── 📄 elevenlabs.py     ☁️ Cloud ElevenLabs Scribe API runner
 │    └── 📁 ui/                    🎨 Interface module
 │         ├── 📄 theme.py          🎨 Color tokens & dark ttk clam styles
 │         ├── 📄 widgets.py        🖼️ Custom Canvas Cards, Sliders, Switches & Transcript
 │         └── 📄 app.py            🖥️ Main Tkinter window controller
 └── 📁 legacy/                     📜 Archived single-file implementation
```

---

## 🗣️ How Speaker Diarization Works

Instead of downmixing audio upfront and estimating speakers probabilistically, both audio channels are captured and transcribed **independently**. Speaker attribution is then determined deterministically in `diarize.py`:

1. 🔇 **Crosstalk / Bleed Filtering**: Speaker audio bleeding into the microphone is filtered using relative track energy levels (independent of gain sliders).
2. 🚫 **Hallucination Prevention**: Audio segments with no signal energy on their source track are automatically dropped.
3. 🔀 **Duplicate Resolution**: Identical sentences detected on both tracks are merged, giving priority to the clearer audio source.

---

## 🎛️ User Interface & Controls

- ⏺️ **Record / Stop (`F5`)**: Start or stop recording at any time with global hotkey support.
- 🎚️ **Gain Sliders**: Adjust audible mixdown levels independently from -20.0 dB to +20.0 dB.
- 💬 **Transcript Pane**: Color-coded speaker text — `[You]` in **blue**, `[Participant]` in **green**, and timestamps in **gray**.
- 💾 **Automatic Output**: Clean WAV audio and plain-text transcripts (`.txt`) are automatically saved to `output/`.

---

## ⚙️ Technical Highlights

- **WASAPI Loopback**: Native Windows WASAPI API capture guarantees zero-loss system audio recording.
- **Polyphase Resampling**: High-quality 16 kHz Mono resampling via `scipy.signal.resample_poly`.
- **Atomic File Operations**: Safe binary downloads and atomic settings updates prevent file corruption.
- **Vulkan / GPU Notice**: By default, GPU flags (`-ng`) fall back to CPU execution to prevent driver crashes on unsupported hardware. Compatible builds can enable GPU acceleration via `allow_gpu`.

---

## 📄 License & FOSS Status

This project is **100% Free and Open Source Software (FOSS)** released under the permissive **[MIT License](LICENSE)**.

```text
MIT License - Copyright (c) 2026 SecretLUL

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so.
```

You are free to use, modify, distribute, and integrate this software in personal or commercial projects.
