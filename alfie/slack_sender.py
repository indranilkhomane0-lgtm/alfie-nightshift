import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

def send_to_slack(signal):
    webhook = os.getenv('SLACK_WEBHOOK')
    if not webhook or webhook.startswith('https://hooks.slack.com/services/YOUR'):
        print("⚠ Slack webhook not configured yet (skip for now)")
        return False
    
    message = {
        "text": f"🚀 *Alfie Signal: {signal['signal']}*",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Signal:* {signal['signal']}\n*Regime:* {signal['regime']}\n*Confidence:* {signal['confidence']:.0%}"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"`{signal['timestamp']}`"}}
        ]
    }
    
    try:
        r = requests.post(webhook, json=message, timeout=5)
        return r.status_code == 200
    except:
        return False

if __name__ == '__main__':
    test_signal = {'signal': 'BUY', 'regime': 'TRENDING', 'confidence': 0.88, 'timestamp': '2024-01-15 08:00:00'}
    result = send_to_slack(test_signal)
    print(f"✓ Slack test: {result}")
