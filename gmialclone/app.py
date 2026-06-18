import os
import re
import threading
import time
from flask import Flask, jsonify, request, render_template, session, redirect
import smtplib
import imaplib
import email
from email.policy import default
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)

# Secret key used to encrypt user sessions. 
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "super-secret-dynamic-key-abc-123"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_SERVER = "imap.gmail.com"

GMAIL_FOLDER_MAP = {
    "inbox": "INBOX",
    "sent": '"[Gmail]/Sent Mail"',
    "starred": '"[Gmail]/Starred"',
    "drafts": '"[Gmail]/Drafts"',
    "snoozed": '"[Gmail]/Snoozed"',
    "trash": '"[Gmail]/Trash"'
}

_imap_lock = threading.Lock()
_imap_conn_pool = {}  # Tracks open connections per user email
_body_cache = {}
_metadata_cache = {}
_last_fetch_time = {}

CACHE_TTL = 8 

def get_user_imap(user_email, app_password):
    """Maintains a persistent live IMAP connection unique to each logged-in user."""
    conn = _imap_conn_pool.get(user_email)
    if conn is not None:
        try:
            conn.noop()
            return conn
        except Exception:
            try:
                conn.logout()
            except Exception:
                pass
            _imap_conn_pool[user_email] = None

    conn = imaplib.IMAP4_SSL(IMAP_SERVER)
    conn.login(user_email, app_password)
    _imap_conn_pool[user_email] = conn
    return conn


def parse_body_from_bytes(raw_bytes):
    try:
        msg = email.message_from_bytes(raw_bytes, policy=default)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_content()
                    break
        else:
            body = msg.get_content()
        return body.strip() if body else ""
    except Exception:
        return ""


@app.route('/')
def home():
    if "user_email" not in session or "app_password" not in session:
        return render_template('login.html')
    return render_template('index.html')


@app.route('/api/login', methods=['POST'])
def handle_login_verification():
    data = request.json or {}
    user_email = data.get("email", "").strip()
    app_password = data.get("password", "").strip()

    if not user_email or not app_password:
        return jsonify({"status": "error", "message": "Email and App Password are required."}), 400

    try:
        # Fast probe check to verify credentials with Google before authorizing session
        test_mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        test_mail.login(user_email, app_password)
        test_mail.logout()

        # Save credentials into cookie session securely
        session["user_email"] = user_email
        session["app_password"] = app_password
        
        # Initialize isolated cache structures for this specific user
        if user_email not in _body_cache:
            _body_cache[user_email] = {f: {} for f in GMAIL_FOLDER_MAP}
            _metadata_cache[user_email] = {f: [] for f in GMAIL_FOLDER_MAP}
            _last_fetch_time[user_email] = {f: 0 for f in GMAIL_FOLDER_MAP}

        return jsonify({"status": "success", "message": "Authentication successful"}), 200
    except Exception:
        return jsonify({"status": "error", "message": "Invalid credentials or IMAP access disabled."}), 401


@app.route('/logout')
def handle_logout():
    user_email = session.get("user_email")
    if user_email in _imap_conn_pool:
        try:
            _imap_conn_pool[user_email].logout()
        except Exception:
            pass
        del _imap_conn_pool[user_email]
        
    session.clear()
    return redirect('/')


@app.route('/api/inbox', methods=['GET'])
def get_inbox():
    if "user_email" not in session:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    user_email = session["user_email"]
    app_password = session["app_password"]
    
    target_folder = request.args.get('folder', 'inbox').lower()
    imap_folder = GMAIL_FOLDER_MAP.get(target_folder, "INBOX")
    
    current_now = time.time()
    user_meta = _metadata_cache.get(user_email, {})
    user_times = _last_fetch_time.get(user_email, {})
    
    if user_meta.get(target_folder) and (current_now - user_times.get(target_folder, 0) < CACHE_TTL):
        return jsonify({"emails": user_meta[target_folder], "status": "success"})

    display_emails = []

    try:
        with _imap_lock:
            mail = get_user_imap(user_email, app_password)
            status, _ = mail.select(imap_folder, readonly=True)
            if status != 'OK':
                return jsonify({"emails": [], "status": "success"})

            if target_folder == "starred":
                status, messages = mail.search(None, "FLAGGED")
            else:
                status, messages = mail.search(None, "ALL")
                
            email_ids = messages[0].split()
            recent_ids = email_ids[-12:]
            recent_ids.reverse()

            if not recent_ids:
                _metadata_cache[user_email][target_folder] = []
                return jsonify({"emails": [], "status": "success"})

            id_set = b','.join(recent_ids)
            status, data = mail.fetch(id_set, "(FLAGS BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)])")

            header_dict = {}
            for response_part in data:
                if isinstance(response_part, tuple):
                    header_info = response_part[0]
                    id_match = re.match(rb'(\d+)', header_info)
                    mail_id = int(id_match.group(1).decode()) if id_match else 0
                    if mail_id == 0: continue

                    flags_match = re.search(rb'FLAGS \(([^)]*)\)', header_info)
                    flags = flags_match.group(1).decode() if flags_match else ""
                    
                    is_unread = "\\Seen" not in flags
                    is_starred = "\\Flagged" in flags or "FLAGGED" in flags.upper()

                    msg = email.message_from_bytes(response_part[1], policy=default)

                    if target_folder == "sent":
                        sender_display = f"To: {msg.get('To') or 'Unknown'}"
                        is_unread = False
                    elif target_folder == "drafts":
                        sender_display = f"Draft to: {msg.get('To') or '(No Recipient)'}"
                        is_unread = False
                    else:
                        sender_display = msg.get("From") or "Unknown Sender"

                    if " <" in sender_display:
                        sender_display = sender_display.split(" <")[0].replace('"', '')

                    subject = msg.get("Subject", "(No Subject)")
                    date_str = msg.get("Date", "")
                    
                    time_display = "Active"
                    if date_str:
                        try:
                            time_display = date_str.split(',')[1].split(':')[0][:-3].strip() if ',' in date_str else date_str[:16]
                        except Exception:
                            time_display = "Active"

                    cached_body = _body_cache[user_email][target_folder].get(mail_id)

                    header_dict[mail_id] = {
                        "id": mail_id,
                        "sender": sender_display,
                        "subject": subject,
                        "body": cached_body if cached_body else "Click to open conversation view...", 
                        "unread": is_unread,
                        "starred": is_starred,
                        "time": time_display
                    }

            for mid in recent_ids:
                int_id = int(mid.decode())
                if int_id in header_dict:
                    display_emails.append(header_dict[int_id])

            _metadata_cache[user_email][target_folder] = display_emails
            _last_fetch_time[user_email][target_folder] = current_now

    except Exception as e:
        print(f"IMAP Multi-User Fetch Error: {e}")
        if user_meta.get(target_folder):
            return jsonify({"emails": user_meta[target_folder], "status": "success"})

    return jsonify({"emails": display_emails, "status": "success"})


@app.route('/api/star', methods=['POST'])
def toggle_star_status():
    if "user_email" not in session: return jsonify({"status": "error"}), 401
    mail_id = request.json.get("id")
    folder = request.json.get("folder", "inbox").lower()
    should_star = request.json.get("starred", False)
    
    try:
        with _imap_lock:
            mail = get_user_imap(session["user_email"], session["app_password"])
            mail.select(GMAIL_FOLDER_MAP.get(folder, "INBOX"))
            action = '+FLAGS' if should_star else '-FLAGS'
            mail.store(str(mail_id), action, '\\Flagged')
            _metadata_cache[session["user_email"]][folder] = []
            _metadata_cache[session["user_email"]]["starred"] = []
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/delete', methods=['POST'])
def delete_emails():
    if "user_email" not in session: return jsonify({"status": "error"}), 401
    mail_ids = request.json.get("ids", [])
    folder = request.json.get("folder", "inbox").lower()
    user_email = session["user_email"]
    
    try:
        with _imap_lock:
            mail = get_user_imap(user_email, session["app_password"])
            mail.select(GMAIL_FOLDER_MAP.get(folder, "INBOX"))
            for mid in mail_ids:
                try:
                    mail.copy(str(mid), GMAIL_FOLDER_MAP["trash"])
                    mail.store(str(mid), '+FLAGS', '\\Deleted')
                    if int(mid) in _body_cache[user_email][folder]:
                        del _body_cache[user_email][folder][int(mid)]
                except Exception: pass
            mail.expunge()
            _metadata_cache[user_email][folder] = []
            _metadata_cache[user_email]["starred"] = []
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/body', methods=['GET'])
def get_email_body():
    if "user_email" not in session: return jsonify({"status": "error"}), 401
    mail_id = request.args.get('id')
    target_folder = request.args.get('folder', 'inbox').lower()
    user_email = session["user_email"]
    
    int_id = int(mail_id)
    if int_id in _body_cache[user_email][target_folder]:
        return jsonify({"status": "success", "body": _body_cache[user_email][target_folder][int_id]})

    try:
        with _imap_lock:
            mail = get_user_imap(user_email, session["app_password"])
            mail.select(GMAIL_FOLDER_MAP.get(target_folder, "INBOX"))
            status, data = mail.fetch(str(mail_id), "(RFC822)")
            for response_part in data:
                if isinstance(response_part, tuple):
                    body_text = parse_body_from_bytes(response_part[1])
                    _body_cache[user_email][target_folder][int_id] = body_text
                    return jsonify({"status": "success", "body": body_text})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "error", "message": "Not found"}), 404


@app.route('/api/draft/save', methods=['POST'])
def save_draft():
    if "user_email" not in session: return jsonify({"status": "error"}), 401
    data = request.json
    existing_id = data.get("id")  
    recipient = data.get("to", "")
    subject = data.get("subject", "")
    body = data.get("body", "")
    user_email = session["user_email"]

    msg = MIMEMultipart()
    msg['From'] = user_email
    msg['To'] = recipient
    msg['Subject'] = subject
    msg['Date'] = email.utils.formatdate(localtime=True)
    msg.attach(MIMEText(body, 'plain'))

    try:
        with _imap_lock:
            mail = get_user_imap(user_email, session["app_password"])
            draft_folder = GMAIL_FOLDER_MAP["drafts"]
            if existing_id:
                mail.select(draft_folder)
                mail.store(str(existing_id), '+FLAGS', '\\Deleted')
                mail.expunge()
            mail.append(draft_folder, '\\Draft', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            _metadata_cache[user_email]["drafts"] = [] 
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/send', methods=['POST'])
def send_email():
    if "user_email" not in session: return jsonify({"status": "error"}), 401
    data = request.json
    recipient = data.get("to")
    subject = data.get("subject")
    body = data.get("body")
    existing_id = data.get("id")  
    user_email = session["user_email"]

    msg = MIMEMultipart()
    msg['From'] = user_email
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(user_email, session["app_password"])
        server.sendmail(user_email, recipient, msg.as_string())
        server.quit()

        if existing_id:
            with _imap_lock:
                mail = get_user_imap(user_email, session["app_password"])
                mail.select(GMAIL_FOLDER_MAP["drafts"])
                mail.store(str(existing_id), '+FLAGS', '\\Deleted')
                mail.expunge()

        _metadata_cache[user_email]["drafts"] = []
        _metadata_cache[user_email]["sent"] = []
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)