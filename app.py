import os
import io
import csv
import json
import base64
import numpy as np
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from werkzeug.utils import secure_filename

# face_recognition imports
import face_recognition
from PIL import Image
import uuid

# Local fast face helper
from fast_face import encode_face_fast, compare_encodings_fast

# Twilio imports (optional)
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()  # loads .env if present

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-key")  # change in production

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
ENC_DIR = os.path.join(DATA_DIR, "encodings")
CSV_PATH = os.path.join(DATA_DIR, "registrations.csv")
os.makedirs(ENC_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Twilio config (optional). Provide in .env or environment variables if you want SMS sending.
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "AC732c7e34eea0ad95ff5beaaccf01f")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "4fcc3512393f433d984bfe3c632764ba")
TWILIO_FROM = os.getenv("TWILIO_FROM_NUMBER", "+12769001378")

# Simple admin credentials (override via environment variables in production)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# OTP storage (in-memory). For production use persistent store with expiry (Redis, DB).
otp_store = {}  # phone -> {"otp": "...", "expires": datetime, "purpose":"register"/"login", "temp_user": {...}}

# CSV paths
ELECTIONS_CSV = os.path.join(DATA_DIR, "elections.csv")
CANDIDATES_CSV = os.path.join(DATA_DIR, "candidates.csv")
VOTES_CSV = os.path.join(DATA_DIR, "votes.csv")

# CSV header ensure
if not os.path.exists(CSV_PATH):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "email", "phone", "encoding_file", "image_file", "registered_at"])

if not os.path.exists(ELECTIONS_CSV):
    with open(ELECTIONS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "status", "created_at", "started_at", "ended_at"])

if not os.path.exists(CANDIDATES_CSV):
    with open(CANDIDATES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "election_id", "user_id", "name", "created_at"])

if not os.path.exists(VOTES_CSV):
    with open(VOTES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "election_id", "voter_id", "candidate_id", "created_at"])

def send_otp(phone: str, otp: str):
    """
    Send OTP via Twilio if configured, else fallback to printing OTP to console.
    """
    if TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM:
        try:
            client = Client(TWILIO_SID, TWILIO_TOKEN)
            message = client.messages.create(
                body=f"Your OTP for E-Voting system is: {otp}",
                from_=TWILIO_FROM,
                to=phone
            )
            app.logger.info("Twilio sent message SID: %s", message.sid)
            return True
        except Exception as e:
            app.logger.warning("Twilio error: %s", e)
            # fallback
    # Fallback: print OTP in console (for local testing)
    print(f"[DEBUG] OTP for {phone}: {otp}")
    return False

def generate_otp():
    import random
    return f"{random.randint(1000, 9999)}"

def save_registration_to_csv(reg):
    # reg: dict with id,name,email,phone, encoding_file, image_file
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            reg["id"],
            reg["name"],
            reg["email"],
            reg["phone"],
            reg["encoding_file"],
            reg["image_file"],  # new column
            datetime.utcnow().isoformat()
        ])


def get_encoding_path_for_phone(phone):
    safe = secure_filename(phone)
    return os.path.join(ENC_DIR, f"{safe}.npy")

def decode_base64_image(data_url):
    # data_url like "data:image/png;base64,...."
    if "," in data_url:
        header, b64 = data_url.split(",", 1)
    else:
        b64 = data_url
    image_bytes = base64.b64decode(b64)
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def read_csv_as_dicts(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def append_csv_row(path, fieldnames, row_dict):
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists or os.path.getsize(path) == 0:
            writer.writeheader()
        writer.writerow(row_dict)


def write_csv_rows(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def get_current_election():
    """Return active election dict or None."""
    elections = read_csv_as_dicts(ELECTIONS_CSV)
    active = [e for e in elections if e.get("status") == "active"]
    if not active:
        return None
    # If multiple, pick the most recently started
    active.sort(key=lambda e: e.get("started_at") or "", reverse=True)
    return active[0]

@app.route("/")
def index():
    return render_template("index.html")

# REGISTER: STEP 1 - show form to collect name, email, phone and capture face
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        face_image_b64 = request.form.get("face_image", "")
        if not (name and phone and face_image_b64):
            flash("Name, phone and face capture required.")
            return redirect(url_for("register"))

        # Decode image and compute face encoding (fast helper)
        pil_img = decode_base64_image(face_image_b64)
        encoding = encode_face_fast(pil_img)
        if encoding is None:
            flash("No face detected or could not encode face. Try again.")
            return redirect(url_for("register"))

        # save encoding to file
        reg_id = str(uuid.uuid4())
        encoding_file = f"{reg_id}.npy"
        encoding_path = os.path.join(ENC_DIR, encoding_file)
        np.save(encoding_path, encoding)

        # save actual face image
        image_file = f"{reg_id}.png"
        image_path = os.path.join(ENC_DIR, image_file)
        pil_img.save(image_path)


        # prepare temp user info and send OTP
        # prepare temp user info and send OTP
        otp = generate_otp()
        otp_store[phone] = {
            "otp": otp,
            "expires": datetime.utcnow() + timedelta(minutes=5),
            "purpose": "register",
            "temp_user": {
                "id": reg_id,
                "name": name,
                "email": email,
                "phone": phone,
                "encoding_file": encoding_file,
                "image_file": image_file   # store image filename too
            }
        }
        send_otp(phone, otp)
        session["pending_phone"] = phone
        return redirect(url_for("verify_otp"))

    return render_template("register.html")

# OTP verification (used for register and login)
@app.route("/verify_otp", methods=["GET", "POST"])
def verify_otp():
    phone = session.get("pending_phone")
    if not phone:
        flash("No OTP session pending.")
        return redirect(url_for("index"))

    if request.method == "POST":
        entered = request.form.get("otp", "").strip()
        rec = otp_store.get(phone)
        if not rec:
            flash("OTP not found or expired.")
            return redirect(url_for("index"))
        if datetime.utcnow() > rec["expires"]:
            otp_store.pop(phone, None)
            flash("OTP expired.")
            return redirect(url_for("index"))
        if entered != rec["otp"]:
            flash("Incorrect OTP.")
            return redirect(url_for("verify_otp"))
        # OTP correct
        if rec["purpose"] == "register":
            save_registration_to_csv(rec["temp_user"])
            # keep user logged in minimal
            session["user_id"] = rec["temp_user"]["id"]
            session["user_name"] = rec["temp_user"]["name"]
            otp_store.pop(phone, None)
            session.pop("pending_phone", None)
            flash("Registration successful.")
            return redirect(url_for("dashboard"))
        elif rec["purpose"] == "login":
            # Login OTP verified — now ask for face capture to finalize
            session["login_phone"] = phone
            otp_store.pop(phone, None)
            session.pop("pending_phone", None)
            return redirect(url_for("capture_face_for_login"))
    return render_template("verify_otp.html", phone=phone)

# Helper function to get user by phone
def get_user_by_phone(phone):
    """Get user registration details by phone number."""
    if not os.path.exists(CSV_PATH):
        return None
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get("phone") == phone:
                # Handle old CSV format that might not have image_file column
                if "image_file" not in r:
                    # Try to infer image file from encoding file
                    encoding_file = r.get("encoding_file", "")
                    if encoding_file:
                        base_name = encoding_file.replace(".npy", "")
                        potential_image = f"{base_name}.png"
                        image_path = os.path.join(ENC_DIR, potential_image)
                        if os.path.exists(image_path):
                            r["image_file"] = potential_image
                        else:
                            r["image_file"] = ""
                return r
    return None

# LOGIN: Step 1: request phone to send OTP
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if not phone:
            flash("Enter phone number.")
            return redirect(url_for("login"))
        # Check registration exists (look into CSV)
        found = get_user_by_phone(phone)
        if not found:
            flash("Phone not registered.")
            return redirect(url_for("login"))

        otp = generate_otp()
        otp_store[phone] = {
            "otp": otp,
            "expires": datetime.utcnow() + timedelta(minutes=5),
            "purpose": "login"
        }
        send_otp(phone, otp)
        session["pending_phone"] = phone
        return redirect(url_for("verify_otp"))
    return render_template("login.html")

# After OTP login verified -> capture face and compare
@app.route("/capture_face_for_login", methods=["GET", "POST"])
def capture_face_for_login():
    phone = session.get("login_phone")
    if not phone:
        flash("No login session.")
        return redirect(url_for("login"))
    if request.method == "POST":
        face_image_b64 = request.form.get("face_image", "")
        if not face_image_b64:
            flash("Capture face first.")
            return redirect(url_for("capture_face_for_login"))
        pil_img = decode_base64_image(face_image_b64)
        login_encoding = encode_face_fast(pil_img)
        if login_encoding is None:
            flash("No face detected or could not encode face.")
            return redirect(url_for("capture_face_for_login"))

        # Load registered encoding for phone from CSV
        reg_row = get_user_by_phone(phone)
        if not reg_row:
            flash("Registration not found.")
            return redirect(url_for("login"))

        encoding_file = reg_row["encoding_file"]
        encoding_path = os.path.join(ENC_DIR, encoding_file)
        if not os.path.exists(encoding_path):
            flash("Registered face encoding file missing.")
            return redirect(url_for("login"))

        registered_enc = np.load(encoding_path)
        is_match, distance = compare_encodings_fast(registered_enc, login_encoding, tolerance=0.5)
        if is_match:
            # success
            session["user_id"] = reg_row["id"]
            session["user_name"] = reg_row["name"]
            session.pop("login_phone", None)
            flash(f"Face recognized (distance={distance:.3f}). Logged in.")
            return redirect(url_for("dashboard"))
        else:
            flash(f"Face not recognized (distance={distance:.3f}).")
            return redirect(url_for("capture_face_for_login"))
    # GET: render capture page
    return render_template("capture_face.html", purpose="login")

@app.route("/dashboard")
def dashboard():
    if not session.get("user_id"):
        return redirect(url_for("login"))
    user_id = session.get("user_id")
    # Get full user data for dashboard
    user_data = None
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("id") == user_id:
                    user_data = r
                    # Handle old CSV format that might not have image_file column
                    if "image_file" not in user_data:
                        # Try to infer image file from encoding file
                        encoding_file = user_data.get("encoding_file", "")
                        if encoding_file:
                            base_name = encoding_file.replace(".npy", "")
                            potential_image = f"{base_name}.png"
                            image_path = os.path.join(ENC_DIR, potential_image)
                            if os.path.exists(image_path):
                                user_data["image_file"] = potential_image
                            else:
                                user_data["image_file"] = ""
                    break

    # Election / voting context for user
    current_election = get_current_election()
    user_vote = None
    election_candidates = []
    if current_election:
        all_candidates = read_csv_as_dicts(CANDIDATES_CSV)
        election_candidates = [c for c in all_candidates if c.get("election_id") == current_election["id"]]
        all_votes = read_csv_as_dicts(VOTES_CSV)
        for v in all_votes:
            if v.get("election_id") == current_election["id"] and v.get("voter_id") == user_id:
                user_vote = v
                break

    return render_template(
        "dashboard.html",
        user_name=session.get("user_name"),
        user_data=user_data,
        current_election=current_election,
        election_candidates=election_candidates,
        user_vote=user_vote,
    )

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Simple admin login with username/password (no OTP)."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin"] = True
            flash("Admin login successful.")
            return redirect(url_for("admin_dashboard"))
        else:
            flash("Invalid admin credentials.")
            return redirect(url_for("admin_login"))
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    flash("Admin logged out.")
    return redirect(url_for("index"))


@app.route("/admin/election/start", methods=["POST"])
def admin_start_election():
    if not session.get("admin"):
        flash("Admin login required.")
        return redirect(url_for("admin_login"))

    # Do not start if an election is already active
    if get_current_election():
        flash("An election is already active.")
        return redirect(url_for("admin_dashboard"))

    elections = read_csv_as_dicts(ELECTIONS_CSV)
    election_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    name = request.form.get("name") or f"Election {now[:10]}"

    elections.append(
        {
            "id": election_id,
            "name": name,
            "status": "active",
            "created_at": now,
            "started_at": now,
            "ended_at": "",
        }
    )
    write_csv_rows(
        ELECTIONS_CSV,
        ["id", "name", "status", "created_at", "started_at", "ended_at"],
        elections,
    )
    flash(f"Election '{name}' started.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/election/close", methods=["POST"])
def admin_close_election():
    if not session.get("admin"):
        flash("Admin login required.")
        return redirect(url_for("admin_login"))

    elections = read_csv_as_dicts(ELECTIONS_CSV)
    current = get_current_election()
    if not current:
        flash("No active election to close.")
        return redirect(url_for("admin_dashboard"))

    now = datetime.utcnow().isoformat()
    for e in elections:
        if e.get("id") == current["id"]:
            e["status"] = "closed"
            e["ended_at"] = now
            break
    write_csv_rows(
        ELECTIONS_CSV,
        ["id", "name", "status", "created_at", "started_at", "ended_at"],
        elections,
    )
    flash("Current election has been closed and results are final.")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin")
@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        flash("Admin login required.")
        return redirect(url_for("admin_login"))

    registrations = read_csv_as_dicts(CSV_PATH)
    try:
        registrations.sort(key=lambda r: r.get("registered_at", ""), reverse=True)
    except Exception:
        pass

    # Election context
    current_election = get_current_election()
    elections = read_csv_as_dicts(ELECTIONS_CSV)
    all_candidates = read_csv_as_dicts(CANDIDATES_CSV)
    all_votes = read_csv_as_dicts(VOTES_CSV)

    # Candidates for current election with vote counts
    election_candidates = []
    if current_election:
        for c in all_candidates:
            if c.get("election_id") == current_election["id"]:
                vote_count = sum(
                    1
                    for v in all_votes
                    if v.get("election_id") == current_election["id"]
                    and v.get("candidate_id") == c.get("id")
                )
                c_with_count = dict(c)
                c_with_count["vote_count"] = vote_count
                election_candidates.append(c_with_count)

    # Simple total votes for summary
    total_votes = 0
    if current_election:
        total_votes = sum(1 for v in all_votes if v.get("election_id") == current_election["id"])

    return render_template(
        "admin_dashboard.html",
        registrations=registrations,
        current_election=current_election,
        elections=elections,
        election_candidates=election_candidates,
        total_votes=total_votes,
    )

@app.route("/vote", methods=["GET"])
def vote_page():
    if not session.get("user_id"):
        return redirect(url_for("login"))

    current_election = get_current_election()

    election_candidates = []
    user_vote = None

    if current_election:
        candidates = read_csv_as_dicts(CANDIDATES_CSV)
        election_candidates = [
            c for c in candidates if c.get("election_id") == current_election["id"]
        ]

        votes = read_csv_as_dicts(VOTES_CSV)
        for v in votes:
            if v.get("election_id") == current_election["id"] and v.get("voter_id") == session.get("user_id"):
                user_vote = v
                break

    return render_template(
        "vote.html",
        current_election=current_election,
        election_candidates=election_candidates,
        user_vote=user_vote
    )


@app.route("/vote", methods=["POST"])
def cast_vote():
    """Logged-in voter casts a single vote for an active election."""
    if not session.get("user_id"):
        flash("Login required.")
        return redirect(url_for("login"))

    current_election = get_current_election()
    if not current_election:
        flash("No active election to vote in.")
        return redirect(url_for("dashboard"))

    voter_id = session.get("user_id")
    candidate_id = request.form.get("candidate_id", "").strip()
    if not candidate_id:
        flash("Please select a candidate.")
        return redirect(url_for("dashboard"))

    # Ensure candidate belongs to this election
    candidates = read_csv_as_dicts(CANDIDATES_CSV)
    valid_candidate = None
    for c in candidates:
        if c.get("id") == candidate_id and c.get("election_id") == current_election["id"]:
            valid_candidate = c
            break
    if not valid_candidate:
        flash("Invalid candidate selection.")
        return redirect(url_for("dashboard"))

    votes = read_csv_as_dicts(VOTES_CSV)
    # Enforce one-time voting per election per user
    for v in votes:
        if v.get("election_id") == current_election["id"] and v.get("voter_id") == voter_id:
            flash("You have already voted in this election.")
            return redirect(url_for("dashboard"))

    vote_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    votes.append(
        {
            "id": vote_id,
            "election_id": current_election["id"],
            "voter_id": voter_id,
            "candidate_id": candidate_id,
            "created_at": now,
        }
    )
    write_csv_rows(
        VOTES_CSV,
        ["id", "election_id", "voter_id", "candidate_id", "created_at"],
        votes,
    )
    flash("Your vote has been recorded.")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("index"))

# Utility route to serve user images
@app.route("/user_image/<filename>")
def user_image(filename):
    """Serve user face images with CORS headers for canvas access."""
    image_path = os.path.join(ENC_DIR, filename)
    if os.path.exists(image_path) and filename.endswith(('.png', '.jpg', '.jpeg')):
        from flask import Response
        response = send_file(image_path, mimetype='image/png')
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Methods', 'GET')
        return response
    return "Image not found", 404

@app.route("/admin/candidate/add", methods=["POST"])
def admin_add_candidate():
    if not session.get("admin"):
        flash("Admin login required.")
        return redirect(url_for("admin_login"))

    current_election = get_current_election()
    if not current_election:
        flash("Start an election first.")
        return redirect(url_for("admin_dashboard"))

    candidate_name = request.form.get("candidate_name", "").strip()
    if not candidate_name:
        flash("Candidate name is required.")
        return redirect(url_for("admin_dashboard"))

    candidates = read_csv_as_dicts(CANDIDATES_CSV)

    candidates.append({
        "id": str(uuid.uuid4()),
        "election_id": current_election["id"],
        "user_id": "",  # admin-added
        "name": candidate_name,
        "created_at": datetime.utcnow().isoformat()
    })

    write_csv_rows(
        CANDIDATES_CSV,
        ["id", "election_id", "user_id", "name", "created_at"],
        candidates
    )

    flash("Candidate added successfully.")
    return redirect(url_for("admin_dashboard"))

@app.route("/results")
def election_results():
    elections = read_csv_as_dicts(ELECTIONS_CSV)

    closed = [e for e in elections if e.get("status") == "closed"]
    if not closed:
        flash("No results available yet.")
        return redirect(url_for("dashboard"))

    closed.sort(key=lambda e: e.get("ended_at", ""), reverse=True)
    current = closed[0]

    candidates = read_csv_as_dicts(CANDIDATES_CSV)
    votes = read_csv_as_dicts(VOTES_CSV)

    results = []
    total_votes = 0

    for c in candidates:
        if c["election_id"] == current["id"]:
            count = sum(
                1 for v in votes
                if v["election_id"] == current["id"]
                and v["candidate_id"] == c["id"]
            )
            total_votes += count
            results.append({"name": c["name"], "votes": count})

    results.sort(key=lambda x: x["votes"], reverse=True)

    winner = results[0]
    runner_up = results[1] if len(results) > 1 else {"votes": 0}

    # ✅ ROLE-BASED BACK LINK
    back_url = url_for("admin_dashboard") if session.get("admin") else url_for("dashboard")

    return render_template(
        "results.html",
        election=current,
        candidates=results,
        winner=winner,
        total_votes=total_votes,
        win_percentage=round((winner["votes"] / total_votes) * 100, 2),
        vote_diff=winner["votes"] - runner_up["votes"],
        back_url=back_url
    )
    
if __name__ == "__main__":
    app.run(debug=True)

