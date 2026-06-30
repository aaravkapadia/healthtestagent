# Voice Agent for AI Engineering Challenge

An automated voice agent that calls the test line and simulates realistic patient conversations to find bugs in the AI agent.

## Architecture

The bot uses a FastAPI server with a WebSocket endpoint that Twilio connects to via Media Streams. When a call connects, Twilio streams raw mulaw audio to the server. That audio is forwarded live to Deepgram for speech-to-text. Transcribed agent text is fed to Claude (acting as the patient persona) to generate a natural response, which is then converted to speech via OpenAI TTS, converted back to mulaw, and streamed back through Twilio into the call.

Key design choices: Deepgram's `nova-2` model with mulaw input avoids an audio conversion step on the receive side, keeping latency low. Claude is given a tight persona prompt and a 150-token limit to keep responses short and conversational. OpenAI's `tts-1` with the `nova` voice was chosen for its natural pacing. ngrok is used during development to expose the local server to Twilio's webhooks.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

You also need `ffmpeg` installed for pydub audio conversion:
- macOS: `brew install ffmpeg`
- Ubuntu: `sudo apt install ffmpeg`

### 2. Configure environment

```bash
cp .env.example .env
# Fill in your API keys in .env
```

Required keys:
- `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER` — [Twilio Console](https://console.twilio.com)
- `DEEPGRAM_API_KEY` — [Deepgram Console](https://console.deepgram.com)
- `OPENAI_API_KEY` — [OpenAI Platform](https://platform.openai.com)
- `ANTHROPIC_API_KEY` — [Anthropic Console](https://console.anthropic.com)

### 3. Start ngrok

```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok.io` URL and set it in `.env`:
```
PUBLIC_URL=https://xxxx.ngrok.io
```

### 4. Start the server in a NEW terminal window

The application code lives in `src/`, and the modules import each other by
bare name (e.g. `from scenarios import ...`), so run everything from inside
`src/`:

```bash
cd src
python server.py
```

## Running Calls(run from another NEW terminal window)

Run these from the `src/` directory as well.

### List available scenarios
```bash
cd src
python run_calls.py --list
```

### Run a single scenario
```bash
python run_calls.py --scenario simple_scheduling
```

### Run multiple scenarios
```bash
python run_calls.py --scenario sunday_appointment medication_refill urgent_same_day
```

### Run all scenarios (with 90s delay between calls)
```bash
python run_calls.py --all
```

### Custom delay between calls
```bash
python run_calls.py --all --delay 120
```

## Generating Bug Report

After running calls, analyze all transcripts:

```bash
cd src
python analyze_bugs.py
```

This produces `bug_report.md` with all identified issues categorized by severity.

## Available Scenarios

All scenarios are written for **Pivot Point Orthopaedics**, an orthopedic
specialty clinic whose agent books appointments (new patient evals, follow-ups,
post-op visits) — it does not handle general/primary care.

| ID | Name |
|----|------|
| `simple_scheduling` | New Patient — Knee Pain Eval |
| `sunday_appointment` | Weekend Appointment Edge Case |
| `post_op_followup` | Post-Op Follow-Up Scheduling |
| `insurance_question` | Insurance & Imaging Coverage Inquiry |
| `cancellation` | Appointment Cancellation |
| `urgent_same_day` | Urgent Same-Day Injury |
| `vague_request` | Vague / Unclear Request |
| `interruption_test` | Interruption / Barge-in Test |
| `wrong_department` | Out-of-Scope / Confused Caller |
| `after_hours` | Office Hours Question |
| `multiple_requests` | Multiple Requests in One Call |
| `no_insurance` | Uninsured / Self-Pay Patient |

## Output Files

- `transcripts/call_{sid}_{scenario}.json` — Full conversation transcript
- `recordings/call_{sid}_{scenario}.wav` — Stereo audio recording of the call,
  written locally by the server (left channel = agent, right channel = patient).
  Twilio also records each call (dual channel), retrievable from the Twilio Console.
- `bug_report.md` — Generated bug report
