"""
The Night Shift Orchestrator
Runs all 4 collectors automatically every day.
Start this once. Leave it running. Go live your life.

Schedule (IST):
  6:00 AM  -> Crypto gauge (overnight BTC movement)
  7:00 AM  -> Asia correlator (Asian markets have closed)
  1:30 PM  -> Premarket scanner (US premarket has started)
  9:30 PM  -> After hours tracker (US market has closed)
"""

import schedule
import time
import subprocess
from datetime import date, datetime

def run_collector(script_name, label):
    """Run one collector and show what happened."""
    now = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{now}] Running {label}...")
    print("-" * 40)

    result = subprocess.run(
        ['python3', f'nie/{script_name}'],
        capture_output=False
    )

    if result.returncode == 0:
        print(f"\n✅ {label} complete")
    else:
        print(f"\n❌ {label} failed - check the script")

    print("-" * 40)

def run_crypto():
    run_collector('crypto_gauge.py', 'Crypto Risk Gauge')

def run_asia():
    run_collector('asia_correlator.py', 'Asia Correlator')

def run_premarket():
    run_collector('premarket_scanner.py', 'Premarket Scanner')
    print("\n💡 Premarket done. Open Claude at 3:45 PM and type /nightly-report")

def run_afterhours():
    run_collector('after_hours_tracker.py', 'After Hours Tracker')
    print("\n✅ All data collected for today.")
    print(f"📁 Check reports/data/ for today's files.")

# Schedule everything
schedule.every().day.at("06:00").do(run_crypto)
schedule.every().day.at("07:00").do(run_asia)
schedule.every().day.at("13:30").do(run_premarket)
schedule.every().day.at("21:30").do(run_afterhours)

# Show startup message
print("=" * 55)
print("🌙 NIGHT SHIFT ORCHESTRATOR")
print("=" * 55)
print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()
print("Daily schedule (IST):")
print("  6:00 AM  → Crypto gauge")
print("  7:00 AM  → Asia correlator")
print("  1:30 PM  → Premarket scanner")
print("  9:30 PM  → After hours tracker")
print()
print("At 3:45 PM IST every day:")
print("  Open Claude → type /nightly-report → review draft → send")
print()
print("Press Ctrl+C to stop")
print("=" * 55)

# Run forever
while True:
    schedule.run_pending()
    time.sleep(60)
