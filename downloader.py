import os
import base64
import re
import subprocess
import sys
import threading
import time
import select
import shutil
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# --- CONFIGURATION ---
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
DRIVE_LOCATION = "/Users/aadipatel/Desktop/ExtraDrive1"

# ---------------------------------------------------------
# GMAIL AUTHENTICATION
# ---------------------------------------------------------

def get_gmail_service():
    """Handles OAuth2 authentication and returns the Gmail API service object."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # If no valid credentials, let the user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Save credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            
    return build('gmail', 'v1', credentials=creds)

# ---------------------------------------------------------
# EMAIL PARSING UTILITIES
# ---------------------------------------------------------

def get_email_body(message):
    """Recursively extracts the plain text or HTML body from a Gmail message object."""
    payload = message.get('payload', {})
    
    def extract_text(part):
        mime_type = part.get('mimeType')
        # Check for both plain text and HTML
        if mime_type in ['text/plain', 'text/html']:
            data = part.get('body', {}).get('data', '')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8')
        
        # Recurse if the email is multipart
        if 'parts' in part:
            return "".join([extract_text(subpart) for subpart in part['parts']])
        return ''
    
    extracted_text = extract_text(payload) or message.get('snippet', '')
    return extracted_text

# ---------------------------------------------------------
# FILENAME & DIRECTORY PROCESSING
# ---------------------------------------------------------

def extract_x_pair_from_tgz(filename: str):
    """
    Identifies the X-pair (e.g., X128a_X1d6) from a .tgz filename.
    Used to rename the generic 'pipeline' folders to something meaningful.
    """
    match = re.search(r'(X[A-Za-z0-9]*)_(X[A-Za-z0-9]*)', filename)
    if match:
        return match.group(1), match.group(2)
    return None

def rename_pipeline_dirs_for_project(project_root: str):
    """
    Walks through the downloaded project folder and renames any 'pipeline' 
    directories to their specific X-pair ID based on the .tgz files inside them.
    """
    if not os.path.isdir(project_root):
        print(f"Project root not found: {project_root}")
        return

    for entry in os.listdir(project_root):
        candidate_path = os.path.join(project_root, entry)
        if not os.path.isdir(candidate_path):
            continue

        # Look for .tgz files inside the directory to determine the correct name
        tgz_found = []
        for root, _, files in os.walk(candidate_path):
            for f in files:
                if f.lower().endswith('.tgz'):
                    tgz_found.append(f)
            if tgz_found: break

        if not tgz_found:
            continue

        x_pair = None
        for tgz in tgz_found:
            x_pair = extract_x_pair_from_tgz(tgz)
            if x_pair: break

        if x_pair:
            x_first, x_second = x_pair
            new_name = f"{x_first}_{x_second}"
            new_path = os.path.join(project_root, new_name)

            if os.path.abspath(candidate_path) != os.path.abspath(new_path):
                if os.path.exists(new_path):
                    print(f"Merging {entry} -> {new_name}")
                    for item in os.listdir(candidate_path):
                        shutil.move(os.path.join(candidate_path, item), os.path.join(new_path, item))
                    shutil.rmtree(candidate_path)
                else:
                    shutil.move(candidate_path, new_path)
                    print(f"Renamed: {entry} -> {new_name}")

# ---------------------------------------------------------
# THREADING: STOP LISTENER
# ---------------------------------------------------------

def _start_stop_listener(stop_event: threading.Event):
    """Runs in background to catch 'q' keypress to safely stop the loop."""
    print("Type 'q' + Enter at any time to stop the process.")
    while not stop_event.is_set():
        try:
            if sys.stdin in select.select([sys.stdin], [], [], 0.5)[0]:
                line = sys.stdin.readline()
                if line.strip().lower() == 'q':
                    stop_event.set()
                    return
        except Exception:
            pass

# ---------------------------------------------------------
# MAIN EXECUTION LOOP
# ---------------------------------------------------------

def main():
    service = get_gmail_service()
    project_code = input("Enter ALMA Project Code (e.g., 2024.1.00657.S): ").strip()

    # Search Gmail for the specific project label
    query = f"label:{project_code}"
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    if not messages:
        print(f"No emails found with label: {project_code}")
        return

    print(f"\n--- Starting Download for {project_code} ({len(messages)} files) ---")

    # Start the 'q' listener thread
    stop_event = threading.Event()
    listener = threading.Thread(target=_start_stop_listener, args=(stop_event,), daemon=True)
    listener.start()

    for i, msg in enumerate(messages):
        if stop_event.is_set():
            print("\nStop requested. Exiting...")
            break

        # Fetch and parse email
        message = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        body = get_email_body(message)
        
        # Regex: Finds the wget2 command and the download URL
        # Handles potential backslashes and HTML tags often found in NRAO emails
        match = re.search(r'wget2\s+.*?(https://dl-naasc\.nrao\.edu/anonymous/\d+/[a-f0-9]+/?)', body, re.DOTALL)

        if match:
            # Clean the command string for the terminal
            cmd = match.group(0).replace('\\\n', ' ').replace('\n', ' ').strip()
            cmd = re.sub(r'<[^>]+>', '', cmd) # Remove HTML tags

            print(f"{i+1}. Downloading...")

            # Execute the download command in the terminal
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=DRIVE_LOCATION,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # Wait for process or stop signal
            while proc.poll() is None:
                if stop_event.is_set():
                    proc.terminate()
                    break
                time.sleep(0.1)

            if proc.returncode == 0:
                os.system('clear')
                for completed in range(1, i + 2):
                    print(f"{completed}. Downloaded")
            else:
                print(f"Error on item {i+1}: {proc.stderr.read().decode().strip()}")

        else:
            print(f"Could not find URL in email {i+1}")

    # Post-download: Rename folders
    project_folder = os.path.join(DRIVE_LOCATION, project_code)
    print(f"\n--- Running Folder Cleanup/Rename ---")
    rename_pipeline_dirs_for_project(project_folder)
    print("Done.")

if __name__ == '__main__':
    main()