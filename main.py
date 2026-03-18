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

Drive_Location = "/Users/aadipatel/Desktop/ExtraDrive1"

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


def get_email_body(message):
    payload = message.get('payload', {})
    
    def extract_text(part):
        mime_type = part.get('mimeType')
        # Check for both plain text and HTML
        if mime_type in ['text/plain', 'text/html']:
            data = part.get('body', {}).get('data', '')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8')
        
        # If it's a multipart message, dive into the parts
        if 'parts' in part:
            all_text = ""
            for subpart in part['parts']:
                all_text += extract_text(subpart)
            return all_text
        return ''
    
    extracted_text = extract_text(payload) or message.get('snippet', '')
    return extracted_text


def extract_x_pair_from_tgz(filename: str):
    """Extract pattern X###..._X###... from a tgz filename and return tuple.
    Example: member.uid___A001_X128a_X1d6.session_1.caltables.tgz -> ('X128a', 'X1d6').
    """
    # Search for two X segments separated by underscore in the basename
    match = re.search(r'(X[0-9]+[A-Za-z0-9]*)_(X[0-9]+[A-Za-z0-9]*)', filename)
    if not match:
        return None
    first, second = match.group(1), match.group(2)
    if first.startswith('X') and second.startswith('X'):
        return first, second
    return None


def rename_pipeline_dirs_for_project(project_root: str):
    """Rename pipeline#### directories under project_root to Xfirst/Xsecond based on .tgz names."""
    import shutil

    if not os.path.isdir(project_root):
        print(f"Project root not found for renaming: {project_root}")
        return

    entries = os.listdir(project_root)
    for entry in entries:
        candidate_path = os.path.join(project_root, entry)
        if not os.path.isdir(candidate_path):
            continue

        # Often pipeline folders are named pipelinexxxx. We can rename any folder by tgz metadata.
        tgz_found = []
        for root, _, files in os.walk(candidate_path):
            for f in files:
                if f.lower().endswith('.tgz'):
                    tgz_found.append(f)
            if tgz_found:
                break

        if not tgz_found:
            continue

        x_pair = None
        for tgz in tgz_found:
            x_pair = extract_x_pair_from_tgz(tgz)
            if x_pair:
                break

        if not x_pair:
            print(f"No X-pair found in .tgz names under {candidate_path}, skipping rename.")
            continue

        x_first, x_second = x_pair
        new_relative = f"{x_first}_{x_second}"
        new_path = os.path.join(project_root, new_relative)

        if os.path.abspath(candidate_path) == os.path.abspath(new_path):
            continue

        # If target exists, merge contents instead of error.
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        if os.path.exists(new_path):
            print(f"Target already exists; merging {candidate_path} into {new_path}")
            for item in os.listdir(candidate_path):
                shutil.move(os.path.join(candidate_path, item), os.path.join(new_path, item))
            shutil.rmtree(candidate_path)
        else:
            shutil.move(candidate_path, new_path)
            print(f"Renamed pipeline folder {candidate_path} -> {new_path}")


def main():
    service = get_gmail_service()
    project_code = input("Enter ALMA Project Code (e.g., 2024.1.00657.S): ")

    # Search for messages with the specific label
    query = f"label:{project_code}"
    print(f"Searching with query: {query}")
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    if not messages:
        print(f"No emails found with label: {project_code}")
        print("Make sure the emails are labeled with the project code in Gmail.")
        return

    total = len(messages)
    drive_path = Drive_Location
    
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
        message = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        body = get_email_body(message)
        
        print(f"\n--- Processing email {current_num} ---")
        print(f"Message ID: {msg['id']}")
        print(f"Body length: {len(body)}")
        print(f"Body preview: {body}...")  # First 500 chars
        
        # Regex to find the download URL
        match = re.search(r'wget2\s+.*?(https://dl-naasc\.nrao\.edu/anonymous/\d+/[a-f0-9]+/?)', body, re.DOTALL)

        if match:
            # group(0) is the whole match including 'wget2' and the URL
            # # .replace('\\', '') handles the backslash line breaks often found in these emails
            download_url = match.group(0).replace('\\', '').strip() 

        
        if match:
            # 1. Get the raw match
            raw_command = match.group(0)
            
            # 2. CLEANING: Remove the backslash and the newline that follows it
            # This turns the multi-line email command into a single-line terminal command
            download_url = raw_command.replace('\\\n', ' ').replace('\n', ' ').strip()
            
            # 3. EXTRA SAFETY: Remove any HTML tags that might have been caught (like <pre>)
            download_url = re.sub(r'<[^>]+>', '', download_url)

            print(f"Found URL: {download_url}")
            print(f"Constructed command: {download_url}")

            
            # Print the log in the format you requested
            print(f"{current_num}. Downloading...")

            proc = subprocess.Popen(
                download_url,
                shell=True,
                cwd=drive_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
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
                    error_message = proc.stderr.read().decode('utf-8').strip() if proc.stderr else "No error message"
                    print(f"Error downloading from item {current_num}: Return code {proc.returncode}, Message: {error_message}")
            except Exception as e:
                proc.terminate()
                print(f"Error downloading item {current_num}: {e}")
                if stop_event.is_set():
                    break
        else:
            print(f"Could not find download URL in email {current_num}")
            print(f"Full body: {body}")
            print("--- End of email body ---")

    project_folder = os.path.join(drive_path, project_code)
    print(f"\n--- Renaming pipeline folders under {project_folder} ---")
    rename_pipeline_dirs_for_project(project_folder)
    print("--- Rename pass completed.\n")

if __name__ == '__main__':
    main()