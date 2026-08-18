import os
import re
import json
import html
import logging
import datetime
import base64
from pathlib import Path
import dateparser
import spacy
import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from gspread_formatting import (
    CellFormat, Color, BooleanCondition, 
    ConditionalFormatRule, get_conditional_format_rules
)

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

today_str = datetime.date.today().strftime("%Y-%m-%d")
LOG_FILE = LOGS_DIR / f"email_tracker_{today_str}.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/spreadsheets'
]
CREDS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"
CONFIG_PATH = BASE_DIR / "config.json"

try:
    nlp = spacy.load("en_core_web_sm")
except Exception:
    nlp = None

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
        return json.load(f)

def get_authenticated_services():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())

    gmail_service = build('gmail', 'v1', credentials=creds)
    gs_client = gspread.authorize(creds)
    return gmail_service, gs_client

def fetch_recent_emails(gmail_service, days=7):
    query = f"(category:primary OR is:starred OR to:me) newer_than:{days}d"
    try:
        results = gmail_service.users().messages().list(userId='me', q=query).execute()
        return results.get('messages', [])
    except Exception as e:
        logging.error(f"Failed to fetch emails: {e}")
        return []

def clean_html_text(raw_text):
    if not raw_text:
        return ""
    text = re.sub(r'<!DOCTYPE[^>]*>', '', raw_text, flags=re.IGNORECASE)
    text = re.sub(r'<!--[\s\S]*?-->', '', text)
    text = re.sub(r'<(head|style|script|xml|o:OfficeDocumentSettings)[^>]*>[\s\S]*?</\1>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return ' '.join(text.split())

def parse_email_payload(msg_data):
    headers = msg_data.get('payload', {}).get('headers', [])
    header_dict = {h['name'].lower(): h['value'] for h in headers}
    
    subject = header_dict.get('subject', 'No Subject')
    sender = header_dict.get('from', '')
    to_field = header_dict.get('to', '')
    date_str = header_dict.get('date', '')
    
    body = ""
    payload = msg_data.get('payload', {})
    if 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/plain' and 'data' in part.get('body', {}):
                body += base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
            elif part.get('mimeType') == 'text/html' and not body and 'data' in part.get('body', {}):
                body += base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
    elif 'body' in payload and 'data' in payload['body']:
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')

    cleaned_body = clean_html_text(body)

    return {
        'id': msg_data.get('id'),
        'threadId': msg_data.get('threadId'),
        'subject': subject,
        'sender': sender,
        'to': to_field,
        'date': date_str,
        'body': cleaned_body,
        'labelIds': msg_data.get('labelIds', [])
    }

def is_important(email_data, config):
    sender_lower = email_data['sender'].lower()
    subject_lower = email_data['subject'].lower()
    body_lower = email_data['body'].lower()
    labels = email_data.get('labelIds', [])

    # Filter out marketing and automated spam senders UNLESS starred by user
    promo_blocklist = [
        'no-reply', 'noreply', 'info@twinmind', 'quillbot', 'linkedin', 
        'fruitkart', 'grammarly', 'offers@', 'marketing@', 'newsletter@'
    ]
    if any(b in sender_lower for b in promo_blocklist) and 'STARRED' not in labels:
        return False, None

    # 1. Starred Emails (Guaranteed Inclusion)
    if 'STARRED' in labels:
        if 'case competition' in subject_lower or 'case competition' in body_lower:
            return True, "Case Competition (Starred)"
        return True, "Starred Mail"

    # 2. Great Lakes Comp Com
    if 'compcom@greatlakes.edu.in' in sender_lower:
        return True, "Great Lakes Comp Com"

    # 3. Case Competition Keyword
    if 'case competition' in subject_lower or 'case competition' in body_lower:
        return True, "Case Competition"

    # 4. Due Date / Date / Deadline Keywords
    if any(k in subject_lower or k in body_lower for k in ['due date', 'deadline', 'submission date', 'last date']):
        return True, "Due Date Alert"

    # 5. Exam & Academic Schedules
    if any(k in subject_lower or k in body_lower for k in ['exam schedule', 'end term', 'timetable', 'mandatory attendance']):
        return True, "Exams & Schedule"

    # 6. Direct Personal Emails
    if 'CATEGORY_PERSONAL' in labels or 'IMPORTANT' in labels:
        return True, "Direct Attention"

    return False, None

def extract_deadline(text):
    if not text:
        return "N/A"
    if nlp:
        doc = nlp(text[:1500])
        for ent in doc.ents:
            if ent.label_ in ["DATE", "TIME"]:
                parsed = dateparser.parse(ent.text, settings={'PREFER_DATES_FROM': 'future'})
                if parsed and parsed.date() >= datetime.date.today():
                    return parsed.strftime("%Y-%m-%d")
    return "N/A"

def summarize_text(text):
    if not text:
        return "No content summary available."
    if nlp:
        doc = nlp(text[:2000])
        sents = [s.text.strip() for s in doc.sents if len(s.text.strip()) > 10]
        if sents:
            return " ".join(sents[:2])
    return text[:250] + "..."

def apply_formatting(worksheet):
    try:
        rules = get_conditional_format_rules(worksheet)
        rules.clear()
        red_rule = ConditionalFormatRule(
            ranges=[],
            booleanRule=BooleanCondition('DATE_BEFORE', ['EXACT_DATE', datetime.date.today().isoformat()]),
            format=CellFormat(backgroundColor=Color(0.95, 0.78, 0.78))
        )
        rules.append(red_rule)
        rules.save()
    except Exception as e:
        logging.warning(f"Formatting warning: {e}")

def process_and_update_sheet():
    logging.info("Starting updated email tracker run...")
    config = load_config()
    gmail_service, gs_client = get_authenticated_services()
    
    sheet = gs_client.open_by_key(config['spreadsheet_id'])
    worksheet = sheet.worksheet(config.get('sheet_name', 'Sheet1'))

    worksheet.clear()
    headers = ["Sno", "Project", "Status", "Deadline", "Subject", "Summary"]
    worksheet.append_row(headers)

    messages = fetch_recent_emails(gmail_service, days=config.get('default_lookback_days', 7))
    new_rows = []
    seen_subjects = set()
    sno = 1

    for msg_meta in messages:
        msg_data = gmail_service.users().messages().get(userId='me', id=msg_meta['id']).execute()
        email = parse_email_payload(msg_data)
        
        important, project_category = is_important(email, config)
        if not important:
            continue

        clean_sub_key = email['subject'].strip().lower()
        if clean_sub_key in seen_subjects:
            continue

        gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{email['threadId']}"
        clean_subject = email["subject"].replace('"', '""')
        hyperlink = f'=HYPERLINK("{gmail_url}", "{clean_subject}")'

        deadline = extract_deadline(email['body'])
        status = "ACTION REQUIRED" if deadline != "N/A" or "Comp Com" in project_category or "Starred" in project_category or "Case" in project_category or "Due Date" in project_category else "Pending Review"

        new_rows.append([
            sno,
            project_category,
            status,
            deadline,
            hyperlink,
            summarize_text(email['body'])
        ])
        seen_subjects.add(clean_sub_key)
        sno += 1

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        apply_formatting(worksheet)
        log_msg = f"Successfully populated sheet with {len(new_rows)} target email entries."
        logging.info(log_msg)
        print(log_msg)
    else:
        log_msg = "No matching primary, starred, or case competition emails found."
        logging.info(log_msg)
        print(log_msg)

if __name__ == "__main__":
    process_and_update_sheet()
