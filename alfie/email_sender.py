import smtplib
from email.mime.text import MIMEText
import os
from dotenv import load_dotenv

load_dotenv()

def send_signal_email(recipient_email, signal):
    sender = os.getenv('GMAIL_USER')
    password = os.getenv('GMAIL_PASS')
    
    if not sender or not password:
        print("⚠ Gmail not configured yet (skip for now)")
        return False
    
    subject = f"Alfie Signal: {signal['signal']}"
    body = f"Alfie Daily Signal\n\nSignal: {signal['signal']}\nRegime: {signal['regime']}\nConfidence: {signal['confidence']:.0%}\nTime: {signal['timestamp']}\n\n---\nAlfie Night Shift\nAutonomous Trading Signals"
    
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender, password)
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = recipient_email
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

if __name__ == '__main__':
    test_signal = {'signal': 'BUY', 'regime': 'TRENDING', 'confidence': 0.88, 'timestamp': '2024-01-15 08:00:00'}
    result = send_signal_email('test@example.com', test_signal)
    print(f"✓ Email test: {result}")
