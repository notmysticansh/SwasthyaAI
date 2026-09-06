import os
import math
import requests
from datetime import timedelta

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session
from flask_cors import CORS
from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from flask_dance.contrib.google import make_google_blueprint, google
from flask_dance.consumer import oauth_authorized
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

from models import db, User, Message
from chatbot import ask_groq

load_dotenv()

# Allow HTTP during local development
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

HISTORY_TURNS = 10

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "swasthya-secret-key-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    "sqlite:///healthbot.db",
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["GOOGLE_OAUTH_CLIENT_ID"] = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
app.config["GOOGLE_OAUTH_CLIENT_SECRET"] = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")

# Persistent login across server restarts (30 days validity)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)

db.init_app(app)
CORS(app)

login_manager = LoginManager()
login_manager.login_view = "auth_page"
login_manager.init_app(app)

# Google Blueprint: Flask-Dance handles the callback internally at /login/google/authorized
google_bp = make_google_blueprint(
    client_id=app.config["GOOGLE_OAUTH_CLIENT_ID"],
    client_secret=app.config["GOOGLE_OAUTH_CLIENT_SECRET"],
    scope=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
    redirect_to="dashboard",  # Authorization successful hone ke baad direct dashboard jayega
    reprompt_select_account=True,
)

app.register_blueprint(google_bp, url_prefix="/login")

with app.app_context():
    db.create_all()


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ----------------------------------------------------------------------------
# Google OAuth Signal
# ----------------------------------------------------------------------------
@oauth_authorized.connect_via(google_bp)
def google_logged_in(blueprint, token):
    if not token:
        flash("Failed to log in with Google.", category="error")
        return False

    resp = blueprint.session.get("/oauth2/v2/userinfo")
    if not resp.ok:
        return False

    info = resp.json()
    email = (info.get("email") or "").strip().lower()
    name = info.get("name") or info.get("given_name") or "User"

    if not email:
        return False

    user = User.query.filter_by(email=email).first()

    if not user:
        # Create new user if first time
        user = User(
            name=name,
            email=email,
            password=generate_password_hash(os.urandom(24).hex()),
            language="English"
        )
        db.session.add(user)
        db.session.commit()

    # Persistent login for Google OAuth
    session.permanent = True
    login_user(user, remember=True)

    # Disable default token storage in Flask-Dance since we use Flask-Login
    return False


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("landing.html")


@app.route("/login-page")
def auth_page():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("auth.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


# ----------------------------------------------------------------------------
# Health check
# ----------------------------------------------------------------------------

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "service": "SwasthyaAI API"})


# ----------------------------------------------------------------------------
# Auth (Manual Email/Password)
# ----------------------------------------------------------------------------

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not email or not password:
        return jsonify({"error": "All fields are required"}), 400

    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email already registered"}), 409

    user = User(
        name=name,
        email=email,
        password=generate_password_hash(password),
        language="English"
    )

    db.session.add(user)
    db.session.commit()

    # Persistent login for manual registration
    session.permanent = True
    login_user(user, remember=True)

    return jsonify(
        {
            "message": "User registered successfully",
            "user_id": user.id,
            "name": user.name,
        }
    ), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = User.query.filter_by(email=email).first()

    if not user or not check_password_hash(user.password, password):
        return jsonify({"error": "Invalid email or password"}), 401

    # Persistent login for manual login
    session.permanent = True
    login_user(user, remember=True)

    return jsonify(
        {
            "message": "Login successful",
            "user_id": user.id,
            "name": user.name,
        }
    )


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("home"))


# ----------------------------------------------------------------------------
# Chat Logic
# ----------------------------------------------------------------------------

def build_history(user_id):
    recent = (
        Message.query.filter_by(user_id=user_id)
        .order_by(Message.created_at.desc())
        .limit(HISTORY_TURNS)
        .all()
    )
    recent.reverse()

    lines = []
    for m in recent:
        speaker = "User" if m.role == "user" else "Assistant"
        lines.append(f"{speaker}: {m.content}")

    return "\n".join(lines)


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}

    message = (data.get("message") or "").strip()
    language = data.get("language") or "English"

    if not message:
        return jsonify({"error": "Message is required"}), 400

    history = ""
    if current_user.is_authenticated:
        history = build_history(current_user.id)

    try:
        answer = ask_groq(message, history, language)
    except Exception as e:
        print("GROQ ERROR:", str(e))
        return jsonify(
            {"error": "AI service unavailable", "details": str(e)}
        ), 500

    if current_user.is_authenticated:
        db.session.add(
            Message(user_id=current_user.id, role="user", content=message)
        )
        db.session.add(
            Message(user_id=current_user.id, role="assistant", content=answer)
        )
        db.session.commit()

    return jsonify({"answer": answer, "language": language})


@app.route("/api/history")
@login_required
def history():
    messages = (
        Message.query.filter_by(user_id=current_user.id)
        .order_by(Message.created_at.asc())
        .all()
    )

    return jsonify(
        [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ]
    )


@app.route("/api/history/clear", methods=["POST"])
@login_required
def clear_history():
    Message.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"message": "History cleared"})


@app.route("/api/user")
@login_required
def user():
    return jsonify(
        {
            "id": current_user.id,
            "name": current_user.name,
            "email": current_user.email,
        }
    )

# ----------------------------------------------------------------------------
# Nearby Hospitals
# ----------------------------------------------------------------------------

def calculate_distance(lat1, lon1, lat2, lon2):
    r = 6371.0  # Earth radius in kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(r * c, 2)


@app.route("/api/hospitals", methods=["POST"])
@login_required
def get_nearby_hospitals():
    data = request.get_json(silent=True) or {}
    lat = data.get("lat")
    lon = data.get("lon")
    query_text = (data.get("query") or "").strip()

    headers = {
        "User-Agent": "SwasthyaAI-App/2.0 (healthbot-support@gmail.com)"
    }

    hospitals = []

    # Method 1: If manual city/area text is given
    if query_text:
        try:
            search_query = f"hospital in {query_text}"
            nom_url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(search_query)}&limit=15&addressdetails=1"
            res = requests.get(nom_url, headers=headers, timeout=8)
            
            items = res.json() if res.ok else []
            if not items:
                city_url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query_text)}&limit=1"
                c_res = requests.get(city_url, headers=headers, timeout=8)
                if c_res.ok and c_res.json():
                    lat = float(c_res.json()[0]["lat"])
                    lon = float(c_res.json()[0]["lon"])

            for item in items:
                h_name = item.get("display_name", "").split(",")[0]
                h_lat = float(item.get("lat"))
                h_lon = float(item.get("lon"))
                h_addr = ", ".join(item.get("display_name", "").split(",")[1:4]).strip()
                
                hospitals.append({
                    "name": h_name,
                    "type": "Hospital / Medical Center",
                    "distance_km": "Nearby",
                    "address": h_addr or query_text,
                    "phone": "Available on Maps",
                    "maps_url": f"https://www.google.com/maps/search/?api=1&query={requests.utils.quote(h_name + ' ' + query_text)}"
                })
        except Exception as e:
            print("Nominatim search error:", e)

    # Method 2: If GPS (lat, lon) is available
    if lat and lon and len(hospitals) < 5:
        try:
            lat = float(lat)
            lon = float(lon)
            rev_url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}"
            rev_res = requests.get(rev_url, headers=headers, timeout=6)
            city_name = "hospital"
            if rev_res.ok and rev_res.json():
                addr_obj = rev_res.json().get("address", {})
                city_name = addr_obj.get("city") or addr_obj.get("town") or addr_obj.get("suburb") or addr_obj.get("county") or "hospital"

            nearby_url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote('hospital ' + city_name)}&limit=10&addressdetails=1"
            res2 = requests.get(nearby_url, headers=headers, timeout=8)
            if res2.ok:
                for item in res2.json():
                    h_lat = float(item.get("lat"))
                    h_lon = float(item.get("lon"))
                    dist = calculate_distance(lat, lon, h_lat, h_lon)
                    h_name = item.get("display_name", "").split(",")[0]
                    h_addr = ", ".join(item.get("display_name", "").split(",")[1:4]).strip()
                    
                    hospitals.append({
                        "name": h_name,
                        "type": "Hospital / Care Clinic",
                        "distance_km": f"{dist} km",
                        "address": h_addr,
                        "phone": "Available on Maps",
                        "maps_url": f"https://www.google.com/maps/search/?api=1&query={h_lat},{h_lon}"
                    })
        except Exception as e:
            print("GPS search error:", e)

    # Deduplicate by hospital name
    seen = set()
    unique_hospitals = []
    for h in hospitals:
        if h["name"].lower() not in seen:
            seen.add(h["name"].lower())
            unique_hospitals.append(h)

    # Fallback to ensure the user is NEVER left empty-handed
    if not unique_hospitals:
        target_name = query_text if query_text else "Current Location"
        fallback_query = requests.utils.quote(f"hospitals near {target_name}")
        unique_hospitals = [
            {
                "name": f"Top Emergency & General Hospitals in {target_name}",
                "type": "Emergency Care",
                "distance_km": "Closest",
                "address": f"Central area, {target_name}",
                "phone": "108 / 112 (Emergency)",
                "maps_url": f"https://www.google.com/maps/search/?api=1&query={fallback_query}"
            },
            {
                "name": f"Government & District Hospital",
                "type": "Civil / Government Hospital",
                "distance_km": "In City Limits",
                "address": f"Main Road, {target_name}",
                "phone": "Emergency Available",
                "maps_url": f"https://www.google.com/maps/search/?api=1&query={requests.utils.quote('Civil Hospital ' + target_name)}"
            },
            {
                "name": f"24/7 Multi-Speciality Care Centres",
                "type": "Private Multi-Speciality",
                "distance_km": "Local Area",
                "address": f"City Healthcare Zone, {target_name}",
                "phone": "24/7 Helpline",
                "maps_url": f"https://www.google.com/maps/search/?api=1&query={requests.utils.quote('Multi Speciality Hospital ' + target_name)}"
            }
        ]

    return jsonify({"hospitals": unique_hospitals[:10]})

#-----------------------------------------------------------------------------
#     Run the Flask app
#-----------------------------------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    print("\n" + "=" * 50)
    print("  🌿 SwasthyaAI Server Running!")
    print("  👉 Open in Browser: http://localhost:5000")
    print("=" * 50 + "\n")

    app.run(debug=True, host="localhost", port=5000)