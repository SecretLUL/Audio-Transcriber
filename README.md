# Audio AI Recorder & Transcriber

Captures microphone and system audio separately and produces a meeting transcript with speaker attribution — locally via `whisper.cpp` or in the cloud via the ElevenLabs Scribe API.

## Quick Start

```shell
Start-Recorder.vbs          Double-click to start without a console window
python main.py              Run with console for debugging
```

First-time setup:

```shell
pip install -r requirements.txt
```

`whisper.cpp` and GGML models are automatically downloaded on demand to `bin/`.

## Architecture & Structure

```text
main.py                     Application entry point
audio_transcriber/
    paths.py                Central path resolution (relative to script directory, not CWD)
    config.py               Typed & versioned settings (Schema v2)
    secretstore.py          Secure API key encryption via Windows DPAPI
    events.py               Thread-safe UI event bridge (single queue & event pump)
    diarize.py              Track merging & crosstalk/bleed filtering
    pipeline.py             Post-processing workflow & live preview engine
    audio/
        devices.py          WASAPI device enumeration & loopback matching
        capture.py          Audio recording, disk streaming & drift correction
        dsp.py              Active channel downmix, exact polyphase resampling, RMS levels
    transcribe/
        base.py             Shared interfaces, timestamp parser & Segment data structures
        binaries.py         Atomic binary/model downloader with Zip-Slip protection
        whispercpp.py       Local whisper.cpp backend runner
        elevenlabs.py       Cloud ElevenLabs Scribe backend runner
    ui/
        theme.py            Color tokens, spacing grid & dark clam ttk styles
        widgets.py          Hand-drawn Canvas UI elements (Cards, Switches, Sliders, Meters, Transcript)
        app.py              Main application window
tests/                      Unit & integration test suite (118 tests, zero external dependencies)
legacy/                     Previous single-file implementation (archived for reference)
```

## User Interface & Design System

Dark card-based layout built completely without external UI libraries: `theme.py` maintains all design tokens and configures a `clam`-based ttk style. `widgets.py` renders custom elements directly onto `tk.Canvas` (rounded cards, toggle switches, smooth sliders, peak/RMS meters, animated status pills, buttons with hover/press states). No hardcoded color literals exist in the UI application code.

The transcript pane automatically formats and colors speaker lines — `[You]` in blue, `[Participant]` in green, and timestamps in gray. Pressing `F5` starts or stops recording.

## How Speaker Diarization Works

Both tracks (microphone and loopback system audio) are transcribed **independently**. Speaker attribution is therefore known upfront rather than guessed. Subsequently, `diarize.py` filters the result:

* **Crosstalk / Bleed Filter**: Voice from speakers bleeding into the microphone is discarded based on track-relative levels (removing gain slider dependency).
* **Hallucination Filter**: Segments without meaningful signal energy on their own track are dropped.
* **Duplicate Merging**: When identical text is recognized on both tracks, the clearer/louder track wins. In case of a tie, the system track wins (because crosstalk physically flows only from speakers to microphone, never vice versa).

## Running Tests

```shell
python run_tests.py             Run all unit tests
python run_tests.py dsp         Run specific test module (e.g., tests/test_dsp.py)
python run_tests.py -v          Run with verbose output
```

Hardware capture tests against real WASAPI devices can be explicitly enabled:

```shell
set AUDIO_TRANSCRIBER_HW_TEST=1
python run_tests.py capture_hardware
```

## Technical Notes & Limitations

* **Vulkan / GPU Status**: The pre-built `whisper.cpp` binary may crash on certain AMD GPUs (e.g. Radeon RX 7700 XT) with `0xC0000409` (STATUS_STACK_BUFFER_OVERRUN). By default, GPU acceleration is disabled (`-ng`). When a compatible binary build is present, this can be toggled via `allow_gpu` in `whispercpp.py`.
* **Separate Track Overhead**: Transcribing two independent tracks doubles execution time (local processing is fast; for ElevenLabs cloud API, separate tracks double API usage). This can be toggled via "Separate tracks" in the UI.
* **VAD Configuration**: Voice Activity Detection (VAD) is disabled by default because standard VAD on some builds merges distant speech segments into overly long blocks. Precise timestamps are critical for speaker attribution.
* **Secure Storage**: ElevenLabs API keys are encrypted using Windows DPAPI bound to the current user account.

## Security Notice

In Schema v1, the ElevenLabs API key was stored in clear text in `settings.json`. On the first launch, keys are automatically migrated to encrypted DPAPI storage.
