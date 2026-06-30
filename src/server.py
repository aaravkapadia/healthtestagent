import asyncio
import base64
import json
import os
import time
import wave
import audioop
from datetime import datetime
from pathlib import Path
import httpx
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from dotenv import load_dotenv
from xml.sax.saxutils import escape as xml_escape
import anthropic

load_dotenv()

app = FastAPI()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

active_sessions = {}


class CallSession:
    """Manages state for a single call."""
    def __init__(self, call_sid: str, scenario: dict):
        self.call_sid = call_sid
        self.scenario = scenario
        self.conversation_history = []
        self.transcript_entries = []
        self.audio_chunks = [] 
        self.patient_audio = [] 
        self.stream_start_wall = None  
        self.stream_sid = None
        self.start_time = datetime.now()
        self.is_agent_speaking = False
        self.speech_buffer = "" 

        Path("transcripts").mkdir(exist_ok=True)
        Path("recordings").mkdir(exist_ok=True)

    def add_transcript(self, speaker: str, text: str):
        ts = time.time() - self.start_time.timestamp()
        entry = {"time": f"{ts:.1f}s", "speaker": speaker, "text": text}
        self.transcript_entries.append(entry)
        print(f"[{ts:.1f}s] {speaker}: {text}")

    def save_transcript(self):
        filename = f"transcripts/call_{self.call_sid}_{self.scenario['id']}.json"
        data = {
            "call_sid": self.call_sid,
            "scenario": self.scenario["id"],
            "persona": self.scenario["persona"],
            "goal": self.scenario["goal"],
            "start_time": self.start_time.isoformat(),
            "transcript": self.transcript_entries,
        }
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Transcript saved: {filename}")
        return filename


@app.post("/incoming-call")
async def incoming_call(request: Request):
    """
    Twilio calls this when the call connects.
    Returns TwiML that opens a Media Stream WebSocket back to us.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    scenario_id = request.query_params.get("scenario", "simple_scheduling")

    from scenarios import get_scenario
    scenario = get_scenario(scenario_id)

    print("DEBUG fired")
    print(f"Call connected: {call_sid} | Scenario: {scenario_id}")

    public_url = os.getenv("PUBLIC_URL", "").replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{public_url}/media-stream?scenario={scenario_id}&call_sid={call_sid}"
    ws_url = xml_escape(ws_url)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}">
      <Parameter name="scenario" value="{scenario_id}"/>
    </Stream>
  </Connect>
</Response>"""

    print(f"TwiML: {twiml}")
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """
    Main WebSocket handler for Twilio Media Streams.
    Receives mulaw audio, transcribes, generates responses, speaks back.
    """
    await websocket.accept()

    scenario_id = websocket.query_params.get("scenario", "simple_scheduling")
    call_sid = websocket.query_params.get("call_sid", "unknown")

    from scenarios import get_scenario
    scenario = get_scenario(scenario_id)
    session = CallSession(call_sid, scenario)
    active_sessions[call_sid] = session

    print(f"WebSocket opened for {call_sid}")

    deepgram_ws = await connect_deepgram()

    # Queue for coordinating between audio receive and response generation
    transcript_queue = asyncio.Queue()
    response_lock = asyncio.Lock()

    turn_segments = []

    async def flush_turn():
        utterance = " ".join(turn_segments).strip()
        turn_segments.clear()
        if utterance:
            await transcript_queue.put(utterance)

    async def deepgram_listener():
        """Listen for transcripts and emit one complete turn per utterance."""
        try:
            async for message in deepgram_ws:
                data = json.loads(message)
                mtype = data.get("type")

                if mtype == "Results":
                    alt = data.get("channel", {}).get("alternatives", [{}])[0]
                    text = alt.get("transcript", "").strip()
                    if not text:
                        continue

                    if data.get("is_final"):
                        turn_segments.append(text)
                        session.speech_buffer = ""
                        if data.get("speech_final"):
                            await flush_turn()
                    else:
                        session.speech_buffer = text

                elif mtype == "UtteranceEnd":
                    await flush_turn()
        except Exception as e:
            print(f"Deepgram listener error: {e}")

    async def response_handler():
        """Reply to the receptionist, letting them finish each turn first.

        This is an inbound call, so the clinic answers and greets first — we
        wait for that greeting before the patient speaks. Only if the line
        stays silent do we open the conversation ourselves.
        """
        greeted = False

        while True:
            try:
                timeout = 12.0 if not greeted else 30.0
                agent_text = await asyncio.wait_for(transcript_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if not greeted:
                    opening = await get_patient_response(session, "[The call connected but no one has spoken yet. Politely open the conversation: greet, introduce yourself, and state why you're calling.]")
                    await speak(websocket, session, opening, "PATIENT")
                    greeted = True
                    continue
                print("No agent speech detected for 30s, ending call")
                break

            greeted = True
            if not agent_text:
                continue

            session.add_transcript("AGENT", agent_text)

            async with response_lock:
                patient_response = await get_patient_response(session, agent_text)
                if patient_response:
                    await speak(websocket, session, patient_response, "PATIENT")

                # Check if conversation is naturally complete
                if should_end_call(agent_text, session):
                    await asyncio.sleep(1.5)
                    break

    try:
        await asyncio.gather(
            receive_twilio_audio(websocket, session, deepgram_ws),
            deepgram_listener(),
            response_handler(),
        )
    except Exception as e:
        print(f"Session error: {e}")
    finally:
        session.save_transcript()
        save_recording(session)
        await deepgram_ws.close()
        active_sessions.pop(call_sid, None)
        print(f"Call ended: {call_sid}")


def save_recording(session: CallSession):
    """
    Write a local stereo WAV of the whole conversation:
      left channel  = the AGENT (inbound audio from Twilio)
      right channel = the PATIENT (our TTS), placed at its real timeline offset
    Both legs are 8kHz mulaw; we decode to 16-bit PCM and interleave.
    """
    if not session.audio_chunks and not session.patient_audio:
        print("No audio captured; skipping recording.")
        return

    try:
        agent_pcm = audioop.ulaw2lin(b"".join(session.audio_chunks), 2)
        patient_pcm = bytearray()
        for offset_bytes, mulaw in session.patient_audio:
            pcm = audioop.ulaw2lin(mulaw, 2)
            start = offset_bytes * 2  # mulaw byte -> 2-byte PCM sample
            if start > len(patient_pcm):
                patient_pcm.extend(b"\x00" * (start - len(patient_pcm)))
            patient_pcm[start:start + len(pcm)] = pcm
        patient_pcm = bytes(patient_pcm)
        length = max(len(agent_pcm), len(patient_pcm))
        agent_pcm += b"\x00" * (length - len(agent_pcm))
        patient_pcm += b"\x00" * (length - len(patient_pcm))
        left = audioop.tostereo(agent_pcm, 2, 1, 0)
        right = audioop.tostereo(patient_pcm, 2, 0, 1)
        stereo = audioop.add(left, right, 2)

        filename = f"recordings/call_{session.call_sid}_{session.scenario['id']}.wav"
        with wave.open(filename, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(stereo)
        dur = length / 2 / 8000
        print(f"Recording saved: {filename} ({dur:.1f}s, L=agent R=patient)")
    except Exception as e:
        print(f"Recording save error: {e}")


async def receive_twilio_audio(websocket: WebSocket, session: CallSession, deepgram_ws):
    """Receive audio from Twilio and forward to Deepgram."""
    try:
        async for message in websocket.iter_text():
            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                session.stream_sid = data["start"]["streamSid"]
                session.stream_start_wall = time.time()

                # Twilio strips query-string params from the Stream URL, so the
                # real scenario/call_sid arrive here instead: scenario via the
                # <Parameter> tag (customParameters), call_sid natively on start.
                params = data["start"].get("customParameters", {})
                cp_scenario = params.get("scenario")
                if cp_scenario:
                    from scenarios import get_scenario
                    session.scenario = get_scenario(cp_scenario)
                cp_call_sid = data["start"].get("callSid")
                if cp_call_sid:
                    session.call_sid = cp_call_sid

                print(f"Stream started: {session.stream_sid} | "
                      f"scenario={session.scenario['id']} call_sid={session.call_sid}")

            elif event == "media":
                audio_payload = data["media"]["payload"]
                audio_bytes = base64.b64decode(audio_payload)
                session.audio_chunks.append(audio_bytes)

                await deepgram_ws.send(audio_bytes)

            elif event == "stop":
                print("Stream stopped by Twilio")
                break

    except Exception as e:
        print(f"Audio receive error: {e}")



async def connect_deepgram():
    """Open a WebSocket connection to Deepgram for live transcription."""
    import websockets

    url = (
        "wss://api.deepgram.com/v1/listen"
        "?encoding=mulaw"
        "&sample_rate=8000"
        "&channels=1"
        "&model=nova-2"
        "&punctuate=true"
        "&endpointing=800" 
        "&interim_results=true"
        "&utterance_end_ms=1000"
    )

    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    ws = await websockets.connect(url, extra_headers=headers)
    print("Deepgram connected")
    return ws



async def get_patient_response(session: CallSession, agent_text: str) -> str:
    """Generate the patient's next response using Claude."""
    session.conversation_history.append({
        "role": "user",
        "content": f"[Agent said]: {agent_text}"
    })

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            system=session.scenario["system_prompt"] + "\n\nIMPORTANT: Keep your response to 1-2 short sentences. Speak naturally as this patient would on a phone call. Do not use any stage directions or brackets.",
            messages=session.conversation_history,
        )

        patient_text = response.content[0].text.strip()

        session.conversation_history.append({
            "role": "assistant",
            "content": patient_text
        })

        return patient_text

    except Exception as e:
        print(f"Claude error: {e}")
        return "Sorry, could you repeat that?"



async def speak(websocket: WebSocket, session: CallSession, text: str, speaker: str):
    """Convert text to speech and send audio back through Twilio."""
    if not text or not session.stream_sid:
        return

    session.add_transcript(speaker, text)
    session.is_agent_speaking = True

    try:
        audio_data = await text_to_speech(text)

        mulaw_audio = convert_to_mulaw(audio_data)

        if session.stream_start_wall is not None:
            offset_bytes = int((time.time() - session.stream_start_wall) * 8000)
            session.patient_audio.append((offset_bytes, mulaw_audio))

        chunk_size = 160  
        for i in range(0, len(mulaw_audio), chunk_size):
            chunk = mulaw_audio[i:i + chunk_size]
            payload = base64.b64encode(chunk).decode("utf-8")

            await websocket.send_json({
                "event": "media",
                "streamSid": session.stream_sid,
                "media": {"payload": payload}
            })

            await asyncio.sleep(0.02)  

        await websocket.send_json({
            "event": "mark",
            "streamSid": session.stream_sid,
            "mark": {"name": "speech_end"}
        })

    except Exception as e:
        print(f"TTS/speak error: {e}")
    finally:
        session.is_agent_speaking = False


async def text_to_speech(text: str) -> bytes:
    """Call OpenAI TTS API and return MP3 audio bytes."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "tts-1",
                "input": text,
                "voice": "nova", 
                "response_format": "mp3",
                "speed": 0.95,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return response.content


def convert_to_mulaw(mp3_bytes: bytes) -> bytes:
    """Convert MP3 audio to mulaw 8kHz mono for Twilio."""
    from pydub import AudioSegment
    import io

    audio = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))

    audio = audio.set_frame_rate(8000).set_channels(1).set_sample_width(2)

    pcm_bytes = audio.raw_data

    mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)
    return mulaw_bytes



def should_end_call(agent_text: str, session: CallSession) -> bool:
    """End the call if the agent said goodbye or call is long."""
    end_phrases = ["goodbye", "have a great day", "is there anything else", "take care", "bye"]
    text_lower = agent_text.lower()

    if any(phrase in text_lower for phrase in end_phrases):
        if len(session.transcript_entries) > 4:
            return True

    elapsed = time.time() - session.start_time.timestamp()
    if elapsed > 240:
        return True

    return False


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
