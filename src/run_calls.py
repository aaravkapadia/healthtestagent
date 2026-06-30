"""
Call Runner
Initiates outbound calls via Twilio for each scenario.
Usage:
    python run_calls.py --scenario simple_scheduling
    python run_calls.py --all
    python run_calls.py --scenario sunday_appointment medication_refill
"""

import argparse
import os
import time
from twilio.rest import Client
from dotenv import load_dotenv
from scenarios import get_all_scenario_ids, get_scenario, SCENARIOS

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
TARGET_PHONE_NUMBER = os.getenv("TARGET_PHONE_NUMBER", "+18054398008")
PUBLIC_URL = os.getenv("PUBLIC_URL")


def make_call(scenario_id: str) -> str:
    """Initiate a single outbound call for the given scenario."""
    scenario = get_scenario(scenario_id)
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    twiml_url = f"{PUBLIC_URL}/incoming-call?scenario={scenario_id}"

    print(f"\n{'='*60}")
    print(f"Calling: {TARGET_PHONE_NUMBER}")
    print(f"Scenario: {scenario['name']}")
    print(f"Persona: {scenario['persona']}")
    print(f"Goal: {scenario['goal']}")
    print(f"TwiML URL: {twiml_url}")
    print(f"{'='*60}")

    call = client.calls.create(
        to=TARGET_PHONE_NUMBER,
        from_=TWILIO_PHONE_NUMBER,
        url=twiml_url,
        record=True, 
        recording_channels="dual", 
    )

    print(f"Call initiated: {call.sid}")
    return call.sid


def run_all_scenarios(delay_between_calls: int = 90):
    """Run all scenarios with a delay between calls."""
    scenario_ids = get_all_scenario_ids()
    print(f"Running {len(scenario_ids)} scenarios...")

    for i, scenario_id in enumerate(scenario_ids):
        print(f"\n[{i+1}/{len(scenario_ids)}] Starting: {scenario_id}")
        call_sid = make_call(scenario_id)

        if i < len(scenario_ids) - 1:
            print(f"Waiting {delay_between_calls}s before next call...")
            time.sleep(delay_between_calls)

    print("\nAll calls initiated.")


def main():
    parser = argparse.ArgumentParser(description="Run voice bot test calls")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", nargs="+", help="Scenario ID(s) to run")
    group.add_argument("--all", action="store_true", help="Run all scenarios")
    group.add_argument("--list", action="store_true", help="List available scenarios")
    parser.add_argument("--delay", type=int, default=90, help="Seconds between calls (default: 90)")

    args = parser.parse_args()

    if args.list:
        print("\nAvailable scenarios:")
        for s in SCENARIOS:
            print(f"  {s['id']:<30} {s['name']}")
        return

    if not PUBLIC_URL:
        print("ERROR: PUBLIC_URL not set in .env — run ngrok first")
        return

    if args.all:
        run_all_scenarios(delay_between_calls=args.delay)
    elif args.scenario:
        for scenario_id in args.scenario:
            make_call(scenario_id)
            if len(args.scenario) > 1:
                print(f"Waiting {args.delay}s...")
                time.sleep(args.delay)


if __name__ == "__main__":
    main()
