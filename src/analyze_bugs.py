import json
import os
from pathlib import Path
from datetime import datetime
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def load_transcripts(transcripts_dir: str = "transcripts") -> list:
    """Load all transcript JSON files."""
    transcripts = []
    for path in Path(transcripts_dir).glob("*.json"):
        with open(path) as f:
            data = json.load(f)
            data["filename"] = path.name
            transcripts.append(data)
    return transcripts


def format_transcript_for_analysis(transcript_data: dict) -> str:
    """Format a transcript into readable text for Claude."""
    lines = [
        f"Call ID: {transcript_data['call_sid']}",
        f"Scenario: {transcript_data['scenario']}",
        f"Patient Persona: {transcript_data['persona']}",
        f"Test Goal: {transcript_data['goal']}",
        f"File: {transcript_data['filename']}",
        "",
        "CONVERSATION:",
    ]

    for entry in transcript_data.get("transcript", []):
        lines.append(f"[{entry['time']}] {entry['speaker']}: {entry['text']}")

    return "\n".join(lines)


def analyze_transcript(transcript_data: dict) -> dict:
    """Use Claude to analyze a single transcript for bugs."""
    formatted = format_transcript_for_analysis(transcript_data)

    prompt = f"""You are a QA engineer analyzing a conversation between a patient caller (simulated) and an AI medical receptionist agent.

Analyze this conversation and identify any bugs, quality issues, or behavioral problems with the AGENT's responses.

{formatted}

Look for issues like:
- Scheduling errors (e.g., confirming appointments on days the office is closed)
- Handling failures (e.g., not properly addressing urgent medical concerns)
- Logic errors (e.g., confirming something impossible or contradictory)
- Poor conversation handling (e.g., not understanding intent, getting stuck in loops)
- Missing information (e.g., not asking for required details like DOB or insurance)
- Inappropriate responses (e.g., wrong tone, unhelpful redirects)
- Edge case failures (e.g., not handling ambiguous requests gracefully)

This agent is the phone receptionist for Pivot Point Orthopaedics, an
orthopedic specialty clinic that books appointments and does not handle
general/primary care. Judge it on that basis.

Respond in JSON format only, with this structure:
{{
  "bugs": [
    {{
      "severity": "high|medium|low",
      "title": "One-line description of what went wrong",
      "timestamp": "time in transcript where the bug occurs, e.g. 1:23 or 83.0s",
      "details": "A clear narrative: quote what the agent actually said, explain why it's a problem, and state what it should have done instead. Write it like the example below."
    }}
  ],
  "summary": "1-2 sentence summary of agent performance in this call"
}}

Example of a good `details` value:
"When asked 'Can I come in Sunday at 10am?', the agent responded 'I've scheduled
you for Sunday at 10am' without checking office hours. The practice is closed on
weekends, so this confirms an impossible appointment. It should have told the
patient the office is closed weekends and offered the next available weekday."

If no bugs are found, return an empty bugs array. Be specific and accurate."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    return json.loads(text)


def generate_bug_report(transcripts_dir: str = "transcripts", output_file: str = "bug_report.md"):
    """Generate a full bug report from all transcripts."""
    transcripts = load_transcripts(transcripts_dir)

    if not transcripts:
        print("No transcripts found. Run some calls first.")
        return

    print(f"Analyzing {len(transcripts)} transcripts...")

    all_bugs = []
    call_summaries = []

    for t in transcripts:
        print(f"  Analyzing: {t['filename']}...")
        try:
            result = analyze_transcript(t)
            bugs = result.get("bugs", [])
            for bug in bugs:
                bug["call_sid"] = t["call_sid"]
                bug["scenario"] = t["scenario"]
                bug["file"] = t["filename"]
            all_bugs.extend(bugs)
            call_summaries.append({
                "scenario": t["scenario"],
                "file": t["filename"],
                "summary": result.get("summary", ""),
                "bug_count": len(bugs),
            })
        except Exception as e:
            print(f"    Error analyzing {t['filename']}: {e}")
    severity_order = {"high": 0, "medium": 1, "low": 2}
    all_bugs.sort(key=lambda x: severity_order.get(x.get("severity", "low"), 3))

    with open(output_file, "w") as f:
        f.write(f"# Bug Report\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Total calls analyzed: {len(transcripts)}\n")
        f.write(f"Total bugs found: {len(all_bugs)}\n\n")

        f.write("## Call Summary\n\n")
        f.write("| Scenario | Bugs | File |\n")
        f.write("|----------|------|------|\n")
        for s in call_summaries:
            f.write(f"| {s['scenario']} | {s['bug_count']} | {s['file']} |\n")

        for severity in ["high", "medium", "low"]:
            severity_bugs = [b for b in all_bugs if b.get("severity") == severity]
            if not severity_bugs:
                continue

            f.write(f"\n## {severity.upper()} Severity Bugs ({len(severity_bugs)})\n\n")

            for i, bug in enumerate(severity_bugs, 1):
                where = f"{bug['file']} at {bug.get('timestamp', 'N/A')}"
                f.write(f"### Bug {i}: {bug['title']}\n\n")
                f.write(f"- **Severity:** {bug['severity'].upper()}\n")
                f.write(f"- **Call:** {where} (scenario: {bug['scenario']})\n")
                f.write(f"- **Details:** {bug.get('details', 'N/A')}\n\n")
                f.write("---\n\n")

    print(f"\nBug report saved: {output_file}")
    print(f"Found {len(all_bugs)} bugs across {len(transcripts)} calls")
    print(f"  High: {len([b for b in all_bugs if b.get('severity') == 'high'])}")
    print(f"  Medium: {len([b for b in all_bugs if b.get('severity') == 'medium'])}")
    print(f"  Low: {len([b for b in all_bugs if b.get('severity') == 'low'])}")

    return all_bugs


if __name__ == "__main__":
    generate_bug_report()
