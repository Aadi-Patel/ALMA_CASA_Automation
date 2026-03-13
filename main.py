import os
import base64
import re
import subprocess
import sys
import threading
import time
import select
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def _start_stop_listener(stop_event: threading.Event):
    """Listen for a 'q' (followed by Enter) to stop downloads.

    This runs in a separate thread so the main download loop can continue.
    """
    print("Press 'q' + Enter at any time to stop.")
    while not stop_event.is_set():
        try:
            # Use select for non-blocking stdin reads.
            if sys.stdin in select.select([sys.stdin], [], [], 0.5)[0]:
                line = sys.stdin.readline()
                if not line:
                    continue
                if line.strip().lower() == 'q':
                    stop_event.set()
                    return
        except Exception:
            # Ignore any stdin read issues and keep trying.
            pass


def main():
    service = get_gmail_service()
    project_code = input("Enter ALMA Project Code (e.g., 2024.1.00657.S): ")

    # Search for messages with the specific label
    query = f"label:{project_code}"
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    if not messages:
        print(f"No emails found with label: {project_code}")
        return

    total = len(messages)
    drive_path = "/Users/aadipatel/Desktop/ExtraDrive1" # Ensure this path is correct for Ubuntu
    
    print(f"\n--- Starting Download for {project_code} ---")
    print(f"Total files to process: {total}\n")

    # Start a background listener so the user can hit 'q' + Enter to stop mid-download.
    stop_event = threading.Event()
    listener = threading.Thread(target=_start_stop_listener, args=(stop_event,), daemon=True)
    listener.start()

    for i, msg in enumerate(messages):
        if stop_event.is_set():
            print("\nStop requested. Ending downloads early.")
            break

        current_num = i + 1
        
        # Get full message content
        message = service.users().messages().get(userId='me', id=msg['id']).execute()
        body = message['snippet'] # Snippet usually contains the wget command for ALMA emails
        
        # Regex to find the wget2 command
        # This looks for the string starting with wget2 all the way to the end of the line
        match = re.search(r'wget2\s+.*', body)
        
        if match:
            wget_command = match.group(0)
            
            # Print the log in the format you requested
            print(f"{current_num}. Downloading...")

            proc = subprocess.Popen(
                wget_command,
                shell=True,
                cwd=drive_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )

            try:
                while proc.poll() is None:
                    if stop_event.is_set():
                        proc.terminate()
                        print("\nKill signal received; stopping current download...")
                        break
                    time.sleep(0.1)

                if stop_event.is_set():
                    break

                if proc.returncode == 0:
                    # Logic to show previous ones as 'Downloaded'
                    os.system('clear') # Clears terminal for the updated log look
                    for completed in range(1, current_num + 1):
                        print(f"{completed}. Downloaded")
                else:
                    print(f"Error downloading item {current_num}")
            except Exception as e:
                proc.terminate()
                print(f"Error downloading item {current_num}: {e}")
                if stop_event.is_set():
                    break
        else:
            print(f"Could not find wget2 command in email {current_num}")

if __name__ == '__main__':
    main()