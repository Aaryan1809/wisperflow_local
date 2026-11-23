# LocalFlow

Private, offline push-to-talk dictation for Windows: a local alternative to Wispr Flow.

**Hold Right Ctrl, speak, let go.** Your words are typed into whatever app has focus.
Speech is recognised on your PC by **Whisper large-v3-turbo** (through whisper.cpp).
It needs no internet and no API keys, has no subscription, and nothing leaves your computer.

## Start

1. Double-click **`LocalFlow.bat`**. A microphone icon appears in the system tray.
   It is grey while the model loads (a few seconds) and green when it is ready.
2. Click into any text box, **hold Right Ctrl**, speak, and release.
3. A small pill above the taskbar shows *Listening* and then *Transcribing*, and the text is pasted in.

| Do this | Result |
|---|---|
| Hold the hotkey and talk | Dictates |
| Press Esc while recording | Cancels |
| Use Right Ctrl + another key (a shortcut) | Works normally, and nothing is recorded |
| Right-click the tray icon | Settings, Restart, Start with Windows, Quit |

The first time only, run `setup.ps1` (`LocalFlow.bat` does it for you if it's missing).

## Settings (`config.json`)

Edit the file (tray menu: **Settings**), then choose **Restart** in the tray menu.

| Setting | Default | What it does |
|---|---|---|
| `hotkey` | `"right ctrl"` | Examples: `"right alt"`, `"f9"`, `"ctrl+windows"`, `"caps lock"` |
| `mode` | `"hold"` | `"hold"` = push-to-talk, `"toggle"` = tap once to start and again to stop |
| `language` | `"en"` | `"en"`, `"hi"` (Hindi), `"auto"`, or any [Whisper language code](https://github.com/openai/whisper#available-models-and-languages) |
| `dictionary` | `[]` | Names and jargon to spell correctly, e.g. `["Kubernetes", "Supabase", "LocalFlow"]` |
| `threads` | `8` | CPU threads for recognition (your CPU has 12) |
| `beam_size` | `5` | 5 = most accurate. 1 = a bit faster |
| `speed_boost` | `true` | Only processes the length you actually spoke (about 3x faster on short clips) |
| `skip_silence` | `true` | Ignores silence, which stops Whisper from inventing text like *"Thank you."* |
| `insert_method` | `"paste"` | `"paste"` (fast, puts your old clipboard back afterwards) or `"type"` (types each character) |
| `add_trailing_space` | `true` | Adds a space after each dictation so the next one doesn't run into it |
| `sounds` / `overlay` | `true` | Start/stop beeps and the on-screen pill |
| `min_seconds` / `max_seconds` | `0.4` / `300` | Taps shorter than this are ignored. Recording stops automatically at the max |
| `microphone` | `null` | `null` = Windows default, or part of the device name, e.g. `"Realtek"` |

## Storage and resources

| What | Where | Size |
|---|---|---|
| Code (this folder) | Google Drive | < 100 KB |
| Speech engine (whisper.cpp + OpenBLAS) | `%LOCALAPPDATA%\LocalFlow\bin` | 62 MB |
| Whisper large-v3-turbo model (q5_0) | `%LOCALAPPDATA%\LocalFlow\models` | 574 MB |
| Silence detector (Silero VAD) | same | 0.9 MB |
| Python packages | `%LOCALAPPDATA%\LocalFlow\venv` | ~25 MB |
| **Total** | | **~650 MB** |

- **RAM:** about 1 GB while LocalFlow runs, because the model stays loaded so dictation starts instantly.
- **CPU:** 0% while idle. A burst of a few seconds per dictation.
- **Disk over time:** nothing is saved. Audio is kept in memory only and discarded after transcription.
- **Logs:** they record timings and word counts, never your words, and are capped at 1 MB.

## Speed

On this laptop (Ryzen 5 7535HS, CPU only, on battery), about 2.4 s for a short sentence and 3.9 s for a 10-second one. It is faster when plugged in.
To get under 1 second, the engine would need to run on the RTX 2050. The prebuilt NVIDIA version adds about 1 GB of libraries.

## Troubleshooting

- **Nothing is typed into an app:** that app is probably running as administrator. Windows blocks other programs from typing into it unless LocalFlow also runs as administrator.
- **Words are misspelled:** add them to `dictionary`.
- **The wrong microphone is used:** set `microphone`, or change the default input in Windows Sound settings, then restart LocalFlow.
- **Hindi or Hinglish:** set `"language": "hi"` (outputs Devanagari) or `"auto"`.
- **Reading the logs:** tray menu > *Open log folder* (`localflow.log`, `engine.log`). Or run `LocalFlow-debug.bat` to watch the log live.
- **Testing the engine without the hotkey:** `python localflow.py --transcribe file.wav` (16 kHz mono WAV).
- **Start with Windows:** if it doesn't start at login, make sure this Google Drive folder is set to *Available offline*.

## Uninstall

Quit from the tray, turn off *Start with Windows* first if you enabled it, then delete `%LOCALAPPDATA%\LocalFlow` and this folder.
