# Voice Bot — Loom Walkthrough Guide

A 5-minute guide to the codebase. Walk it in this order: **scenarios → run_calls → server → analyze_bugs**. That follows the actual data flow: define a patient, place the call, run the live conversation, then grade the transcript.

---

## 30-Second Pitch (say this first)

This is an automated **voice bot that QA-tests an AI medical receptionist**. It dials a clinic's test line, plays a realistic patient (powered by Claude), has a real spoken back-and-forth, records the transcript, then uses Claude again to read every transcript and write a categorized bug report. Twelve patient scenarios cover the normal and edge cases.

**Pipeline:** Twilio (phone) → Deepgram (speech-to-text) → Claude (patient brain) → OpenAI TTS (voice) → back through Twilio.

---

## `scenarios.py` — The patient playbook

The test cases. A list of 12 patient personas for **Pivot Point Orthopaedics**, each one a dictionary with an `id`, `name`, `persona`, `goal`, and a `system_prompt` that tells Claude exactly who to be on the call.

- **`SCENARIOS`** — the list of 12 scenario dicts. Most are in-scope orthopedic patients; a few (`wrong_department`, `vague_request`, `sunday_appointment`) are deliberately designed to trip the agent up on edge cases.
- **`get_scenario(scenario_id)`** — looks up one scenario by its ID; raises if it doesn't exist.
- **`get_all_scenario_ids()`** — returns every scenario ID (used by the "run all" mode).

> *Talking point:* the `system_prompt` is the secret sauce — it gives Claude a DOB, insurance, symptoms, and behavioral notes so each call feels like a different real person.

---

## `run_calls.py` — The dialer (CLI entry point)

Kicks off outbound phone calls through Twilio. This is what you run from the terminal.

- **`make_call(scenario_id)`** — places a single Twilio call to the target number, pointing Twilio at our server's webhook URL (with the scenario tagged on). Records the call in dual-channel and returns the call SID.
- **`run_all_scenarios(delay_between_calls)`** — loops through all 12 scenarios, calling each with a delay in between so calls don't overlap.
- **`main()`** — parses CLI args (`--scenario`, `--all`, `--list`, `--delay`) and dispatches accordingly. Also guards that `PUBLIC_URL` (ngrok) is set.

> *Talking point:* Twilio doesn't connect to your laptop directly — it hits the public ngrok URL, which forwards to the local server.

---

## `server.py` — The live conversation engine (the heart)

A FastAPI server that runs the actual real-time phone conversation. When Twilio connects a call, it opens a WebSocket here and streams audio both ways. **Spend the most time here.**

**Session state**
- **`CallSession`** — holds everything for one call: conversation history, transcript entries, captured audio, stream IDs, timing.
  - **`add_transcript(speaker, text)`** — appends a timestamped line to the transcript and prints it live.
  - **`save_transcript()`** — writes the full conversation out to a JSON file when the call ends.

**HTTP + WebSocket endpoints**
- **`incoming_call()`** (`POST /incoming-call`) — Twilio hits this when the call connects. Returns TwiML that tells Twilio to open a Media Stream WebSocket back to us.
- **`media_stream()`** (`WS /media-stream`) — the main handler. Wires together three concurrent async tasks and runs them with `asyncio.gather`:
  - **`deepgram_listener()`** — reads transcription results from Deepgram, assembles them into complete agent turns (what the receptionist said), and pushes each finished utterance onto a queue.
  - **`response_handler()`** — pulls each agent turn off the queue, asks Claude for the patient's reply, speaks it back, and decides when the call is done. Handles the "no one greeted us" timeout by opening the conversation itself.
  - **`flush_turn()`** — helper that joins buffered speech segments into one utterance and queues it.

**The audio + AI helpers (the actual pipeline steps)**
- **`receive_twilio_audio()`** — receives raw mulaw audio chunks from Twilio and forwards them straight to Deepgram for transcription.
- **`connect_deepgram()`** — opens the Deepgram WebSocket configured for 8kHz mulaw, `nova-2` model, with endpointing so it knows when a turn ends.
- **`get_patient_response()`** — sends the conversation to **Claude** with the scenario's persona prompt and gets back the patient's next line (capped short so it sounds natural on a phone).
- **`speak()`** — converts text to audio, chunks it, and streams it back through Twilio into the call; also stores it for the local recording.
- **`text_to_speech()`** — calls **OpenAI TTS** (`tts-1`, "nova" voice) and returns MP3 bytes.
- **`convert_to_mulaw()`** — converts that MP3 into the 8kHz mulaw format Twilio requires (via pydub/ffmpeg).
- **`save_recording()`** — writes a local stereo WAV: agent on the left channel, patient on the right, aligned on the real call timeline.
- **`should_end_call()`** — detects natural endings ("goodbye", "anything else?") or a max call length and tells the loop to hang up.

> *Talking point:* the three async tasks run at once — audio is flowing in, being transcribed, and responses are being generated and spoken simultaneously. That concurrency is what keeps latency low enough to feel like a real call.

---

## `analyze_bugs.py` — The grader / report generator

After the calls, this reads every transcript and uses Claude as a **QA engineer** to find bugs, then writes a Markdown report sorted by severity.

- **`load_transcripts()`** — loads every transcript JSON from the `transcripts/` folder.
- **`format_transcript_for_analysis()`** — turns one transcript into clean readable text (persona, goal, conversation) for Claude.
- **`analyze_transcript()`** — sends one conversation to Claude and gets back structured JSON: a list of bugs (severity, title, timestamp, details), an overall quality rating, and a summary.
- **`generate_bug_report()`** — runs that analysis across all transcripts, sorts bugs high→low severity, and writes `bug_report.md` with a summary table and a section per severity level.

> *Talking point:* Claude is used twice in this project — once to **act** as the patient (server.py), and once to **judge** the agent (analyze_bugs.py).

---

## Suggested Loom Flow (≈5 min)

1. **0:00–0:30** — The 30-second pitch + the pipeline diagram above.
2. **0:30–1:15** — `scenarios.py`: open one `system_prompt`, show how a persona is defined.
3. **1:15–2:00** — `run_calls.py`: show the CLI commands, how a call gets placed.
4. **2:00–4:00** — `server.py`: the three async tasks and the audio→Deepgram→Claude→TTS→Twilio loop. **This is the centerpiece.**
5. **4:00–4:45** — `analyze_bugs.py`: how transcripts become a severity-ranked bug report.
6. **4:45–5:00** — Show a sample `bug_report.md` / a recording, wrap up.
