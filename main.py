# merged main.py
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
import pandas as pd
from urllib.parse import urlparse
import ast
import json
import math
import re
from datetime import datetime
import time
import firebase_admin
from firebase_admin import credentials, auth, firestore, storage as firebase_storage
from pydantic import BaseModel, EmailStr
from typing import List, Optional, Dict, Any
import os
from openai import OpenAI
import requests
import io
import tempfile
import signal
import asyncio
from contextlib import contextmanager
import time
import threading
import csv
import logging

# ----------------------------
# Basic configuration
# ----------------------------
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", 60 * 60 * 24))  # default 24 hours
FIREBASE_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "/app/firebase-key.json")
FRONTEND_URL = os.getenv("FRONTEND_URL")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("campus-connect")

# Initialize FastAPI app
app = FastAPI(
    title="Campus Connect API (Merged)",
    description="Backend API for Campus Connect application",
    version="1.0.0"
)

# CORS middleware - Allow all origins for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Security - Configure HTTPBearer
# auto_error=False allows us to handle missing credentials with better error messages
security = HTTPBearer(auto_error=False)

# ----------------------------
# Timeout context manager (async-compatible version)
# ----------------------------
class TimeoutException(Exception):
    pass

@contextmanager
def timeout(seconds):
    """
    DEPRECATED: Signal-based timeout doesn't work reliably in production.
    Use asyncio.wait_for() for async functions instead.
    Kept for backward compatibility but should not be used.
    """
    logger.warning("Signal-based timeout() is deprecated and may not work in production")
    try:
        # On systems that support SIGALRM
        def signal_handler(signum, frame):
            raise TimeoutException(f"Timed out after {seconds} seconds")
        signal.signal(signal.SIGALRM, signal_handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
    except AttributeError:
        # SIGALRM not available (Windows, some containers)
        logger.error("SIGALRM not available on this platform, timeout will not work")
        yield

# ----------------------------
# Firebase initialization (merged, robust)
# ----------------------------
_db = None
_default_bucket = None
_firebase_lock = threading.Lock()

def initialize_firebase():
    """Lazy initialize Firebase Admin (Firestore + Storage)."""
    global _db, _default_bucket
    with _firebase_lock:
        if _db is not None:
            return _db

        try:
            if os.path.exists(FIREBASE_KEY_PATH):
                logger.info(f"Initializing Firebase from file: {FIREBASE_KEY_PATH}")
                cred = credentials.Certificate(FIREBASE_KEY_PATH)
                firebase_admin.initialize_app(cred)
            else:
                # Try env var config (private key etc.)
                project_id = os.getenv("FIREBASE_PROJECT_ID")
                private_key = os.getenv("FIREBASE_PRIVATE_KEY")
                client_email = os.getenv("FIREBASE_CLIENT_EMAIL")
                if project_id and private_key and client_email:
                    logger.info("Initializing Firebase from environment variables")
                    key_dict = {
                        "type": "service_account",
                        "project_id": project_id,
                        "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
                        "private_key": private_key.replace("\\n", "\n"),
                        "client_email": client_email,
                        "client_id": os.getenv("FIREBASE_CLIENT_ID"),
                    }
                    cred = credentials.Certificate(key_dict)
                    firebase_admin.initialize_app(cred)
                else:
                    # Try default credentials (e.g. on GCP)
                    logger.info("Initializing Firebase with default credentials")
                    firebase_admin.initialize_app()
        except Exception as e:
            logger.exception("Failed to initialize Firebase Admin SDK")
            _db = None
            _default_bucket = None
            return None

        try:
            _db = firestore.client()
            # Try to set default storage bucket if env var set
            bucket_name = os.getenv("FIREBASE_STORAGE_BUCKET")
            if bucket_name:
                _default_bucket = firebase_storage.bucket(bucket_name)
            else:
                try:
                    _default_bucket = firebase_storage.bucket()
                except Exception:
                    _default_bucket = None
            logger.info("Firebase initialized successfully")
            return _db
        except Exception as e:
            logger.exception("Failed to initialize Firestore or Storage")
            _db = None
            _default_bucket = None
            return None

def get_db():
    """Compatibility wrapper (original used get_db)"""
    global _db
    if _db is not None:
        return _db
    return initialize_firebase()

# Ensure firebase is initialized at import (optional)
# We call initialize_firebase lazily in endpoints; but initializing here surfaces early errors.
try:
    initialize_firebase()
except Exception:
    pass

# ----------------------------
# In-memory per-university cache (thread-safe)
# ----------------------------
_university_cache_lock = threading.Lock()
_university_cache: Dict[str, Dict[str, Any]] = {}  # short_hand -> {loaded_at, data: {key: obj}, errors}

# Today's events cache with date-indexed lookup
_today_events_cache_lock = threading.Lock()
_today_events_cache: Dict[str, Dict[str, Any]] = {}  # short_hand -> {date: str, events: list, loaded_at: float}
TODAY_EVENTS_CACHE_TTL = 300  # 5 minutes cache for today's events

# ----------------------------
# Helper: Parse event date efficiently
# ----------------------------
def _parse_event_date(event: dict) -> Optional[datetime]:
    """Extract and parse date from event dict with multiple field name fallbacks."""
    date_str = (event.get('date') or 
               event.get('Date') or 
               event.get('start_date') or 
               event.get('Start Date') or
               event.get('event_date'))
    
    if not date_str:
        return None
    
    try:
        # Handle both "YYYY-MM-DD" and "YYYY-MM-DD HH:MM:SS" formats
        date_part = str(date_str).split('T')[0].split(' ')[0]
        return datetime.strptime(date_part, '%Y-%m-%d')
    except Exception:
        return None

# ----------------------------
# Utilities: fetch files (gs://, http(s), local)
# ----------------------------
def _parse_gs_path(gs_path: str):
    # gs://bucket/path/to/file
    assert gs_path.startswith("gs://")
    without = gs_path[5:]
    parts = without.split("/", 1)
    bucket = parts[0]
    blob = parts[1] if len(parts) > 1 else ""
    return bucket, blob

def fetch_remote_binary(path: str) -> bytes:
    """
    Fetch binary content from gs://bucket/path, https://... or local filesystem path.
    Returns bytes.
    """
    path = path.strip()
    if path.startswith("gs://"):
        bucket_name, blob_path = _parse_gs_path(path)
        try:
            logger.debug(f"Downloading binary from gs://{bucket_name}/{blob_path}")
            bucket = None
            if _default_bucket and _default_bucket.name == bucket_name:
                bucket = _default_bucket
            else:
                bucket = firebase_storage.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            return blob.download_as_bytes()
        except Exception as e:
            logger.exception(f"Failed to download {path}")
            raise RuntimeError(f"Failed to download from {path}: {e}")
    elif path.startswith("http://") or path.startswith("https://"):
        try:
            r = requests.get(path, timeout=20)
            r.raise_for_status()
            return r.content
        except Exception as e:
            logger.exception(f"HTTP fetch failed for {path}")
            raise RuntimeError(f"HTTP fetch failed {path}: {e}")
    else:
        # treat as local filesystem path
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        raise RuntimeError(f"Unknown/unsupported storage path: {path}")

def fetch_remote_text(path: str) -> str:
    """
    Fetch content from gs://bucket/path, https://... or local filesystem path.
    Returns text (str).
    """
    path = path.strip()
    if path.startswith("gs://"):
        bucket_name, blob_path = _parse_gs_path(path)
        try:
            logger.debug(f"Downloading from gs://{bucket_name}/{blob_path}")
            bucket = None
            if _default_bucket and _default_bucket.name == bucket_name:
                bucket = _default_bucket
            else:
                bucket = firebase_storage.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            return blob.download_as_text()
        except Exception as e:
            logger.exception(f"Failed to download {path}")
            raise RuntimeError(f"Failed to download from {path}: {e}")
    elif path.startswith("http://") or path.startswith("https://"):
        try:
            r = requests.get(path, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            logger.exception(f"HTTP fetch failed for {path}")
            raise RuntimeError(f"HTTP fetch failed {path}: {e}")
    else:
        # treat as local filesystem path
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        raise RuntimeError(f"Unknown/unsupported storage path: {path}")

def parse_content_by_extension(path: str, text: str):
    """
    Parse JSON and CSV. CSV parsed into list[dict].
    If file extension unknown, try JSON then return raw text.
    """
    lower = path.lower()
    if lower.endswith(".json"):
        return json.loads(text)
    if lower.endswith(".csv"):
        # CSV -> list of dicts
        reader = csv.DictReader(io.StringIO(text))
        return [row for row in reader]
    if lower.endswith(".tsv"):
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        return [row for row in reader]
    # default: try JSON
    try:
        return json.loads(text)
    except Exception:
        return text

# ----------------------------
# Load + cache university data (from rewrite, adapted)
# ----------------------------
def load_university_data(short_hand: str, force: bool = False, max_rows: int = 5000) -> Dict[str, Any]:
    """
    Load and cache parsed data for a university.
    Expects Firestore doc at /university/{short_hand} with a `data` map:
        data: {
            "calendar": "gs://bucket/..../calendar.json",
            "events": "gs://bucket/..../events.csv",
            ...
        }
    Returns parsed map: {key: parsed_content_or_None}
    
    NOTE: This file-based approach is deprecated and causes performance issues.
    See MIGRATION_TO_FIRESTORE.md for migration to Firestore collections.
    
    Args:
        short_hand: University short code
        force: Force reload ignoring cache
        max_rows: Maximum rows to load from files (prevents memory issues)
    """
    if not short_hand:
        raise HTTPException(status_code=400, detail="short_hand required")

    short_hand = short_hand.lower()
    now = time.time()

    with _university_cache_lock:
        entry = _university_cache.get(short_hand)
        if entry and not force and (now - entry.get("loaded_at", 0)) < CACHE_TTL_SECONDS:
            logger.debug(f"Cache hit for {short_hand}")
            return entry["data"]

    db = initialize_firebase()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")

    uni_doc = db.collection("university").document(short_hand).get()
    if not uni_doc.exists:
        # Do not raise here: return empty map to allow fallback to local files
        logger.warning(f"University {short_hand} not found in Firestore")
        parsed = {}
        with _university_cache_lock:
            _university_cache[short_hand] = {"loaded_at": now, "data": parsed, "errors": {"_global": "university doc not found"}}
        return parsed

    uni = uni_doc.to_dict()
    # accept both "data" and older "data_files" keys
    data_map = uni.get("data") or uni.get("data_files") or {}
    parsed: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for key, path in (data_map.items() if isinstance(data_map, dict) else []):
        if not path:
            parsed[key] = None
            continue
        try:
            # If Excel files, fetch binary and parse with pandas
            if isinstance(path, str) and path.lower().endswith((".xlsx", ".xls")):
                logger.info(f"Fetching Excel file for {key}: {path}")
                content = fetch_remote_binary(path)
                # try to parse with pandas if available
                try:
                    df = pd.read_excel(io.BytesIO(content), engine="openpyxl")
                    
                    # PERFORMANCE FIX: Limit rows to prevent memory issues
                    original_rows = len(df)
                    if original_rows > max_rows:
                        logger.warning(f"⚠️  Truncating {key} from {original_rows} to {max_rows} rows to prevent memory issues")
                        logger.warning(f"⚠️  Consider migrating to Firestore collections - see MIGRATION_TO_FIRESTORE.md")
                        df = df.head(max_rows)
                    
                    parsed[key] = df.to_dict(orient="records")
                    logger.info(f"Successfully parsed Excel file for {key}: {len(parsed[key])} rows")
                except Exception as e:
                    logger.error(f"Excel parsing failed for {key}: {e}")
                    errors[key] = f"Excel parsing failed: {e}"
                    parsed[key] = None
                continue

            # For other file types, fetch as text
            text = fetch_remote_text(path)
            parsed_obj = parse_content_by_extension(path, text)
            
            # PERFORMANCE FIX: Limit rows for CSV files
            if isinstance(parsed_obj, list) and len(parsed_obj) > max_rows:
                original_rows = len(parsed_obj)
                logger.warning(f"⚠️  Truncating {key} from {original_rows} to {max_rows} rows to prevent memory issues")
                logger.warning(f"⚠️  Consider migrating to Firestore collections - see MIGRATION_TO_FIRESTORE.md")
                parsed_obj = parsed_obj[:max_rows]
            
            parsed[key] = parsed_obj
        except Exception as e:
            logger.exception(f"Failed to load {key} from {path}")
            errors[key] = str(e)
            parsed[key] = None

    with _university_cache_lock:
        _university_cache[short_hand] = {
            "loaded_at": now,
            "data": parsed,
            "errors": errors,
        }

    return parsed


# ----------------------------
# Helper: get user's short_hand from Firestore (uid or email)
# ----------------------------
def get_user_short_hand_from_firestore(uid_or_email: str) -> Optional[str]:
    db = initialize_firebase()
    if not db:
        return None
    try:
        doc = db.collection("users").document(uid_or_email).get()
        if doc.exists:
            d = doc.to_dict()
            return d.get("short_hand") or d.get("university") or d.get("school_short") or None
    except Exception:
        logger.exception("Failed reading user doc for short_hand")
    return None

# ----------------------------
# Authentication dependency with UID logging
# ----------------------------
async def get_current_user_optional(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """
    Optional authentication - returns user info if token provided, None otherwise.
    Does not raise exceptions for missing credentials.
    """
    if credentials is None:
        return None
    
    try:
        firebase_admin.get_app()
    except ValueError:
        return None
    
    try:
        token = credentials.credentials
        if not token or not token.strip():
            return None
        
        decoded_token = auth.verify_id_token(token)
        firebase_uid = decoded_token.get("uid") or decoded_token.get("user_id") or decoded_token.get("sub")
        user_email = decoded_token.get("email")
        
        if firebase_uid:
            logger.info(f"✅ Authenticated request from Firebase UID: {firebase_uid}, Email: {user_email}")
        
        return decoded_token
    except Exception as e:
        logger.warning(f"Optional auth failed: {str(e)}")
        return None

async def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """
    Authenticate requests using Firebase ID tokens and log the Firebase UID.
    Returns 403 for authentication failures.
    Requires Authorization header: Bearer <ID_TOKEN>
    """
    # Check if credentials were provided
    if credentials is None:
        logger.error("No Authorization header provided in request")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authentication required. Please provide a valid Firebase ID token in the Authorization header as: Bearer <ID_TOKEN>"
        )
    
    # Check if Firebase Admin is initialized
    try:
        firebase_admin.get_app()
    except ValueError:
        logger.error("Firebase Admin SDK is not initialized")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service is not available. Please contact support."
        )
    
    try:
        token = credentials.credentials
        if not token or not token.strip():
            logger.error("Empty token provided")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid token. Please provide a valid Firebase ID token in the Authorization header."
            )
        
        # Verify the Firebase ID token
        decoded_token = auth.verify_id_token(token)
        
        # Extract and log Firebase UID
        firebase_uid = decoded_token.get("uid") or decoded_token.get("user_id") or decoded_token.get("sub")
        user_email = decoded_token.get("email")
        
        if firebase_uid:
            logger.info(f"✅ Authenticated request from Firebase UID: {firebase_uid}, Email: {user_email}")
            print(f"[AUTH] ✅ Firebase UID: {firebase_uid}, Email: {user_email}")
        else:
            logger.warning("Firebase token verified but no UID found in token")
            print(f"[AUTH] ⚠️  Warning: No UID found in token")
        
        return decoded_token
        
    except ValueError as e:
        logger.error(f"ValueError in token verification: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Invalid token format: {str(e)}. Please ensure you're using a valid Firebase ID token."
        )
    except firebase_admin.exceptions.InvalidArgumentError as e:
        logger.error(f"Invalid Firebase token provided: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication credentials. Please check your Firebase ID token is valid and not expired."
        )
    except firebase_admin.exceptions.ExpiredIdTokenError:
        logger.error("Expired Firebase token provided")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token has expired. Please refresh your Firebase ID token."
        )
    except Exception as e:
        logger.error(f"Authentication error: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Authentication failed: {str(e)}"
        )

# ----------------------------
# Original Pydantic models (kept)
# ----------------------------
class UserProfile(BaseModel):
    uid: str  # Firebase UID
    name: str
    surname: str
    school_name: str
    year: str
    ftcs_status: str
    gpa_range: str
    educational_goals: str
    age: str
    gender: str
    race_ethnicity: str
    working_hours: str
    stress_level: str
    self_efficacy: str
    major: str
    interests: List[str]
    email: EmailStr
    high_school_grades: Optional[str] = None
    financial_factors: Optional[str] = None
    family_responsibilities: Optional[str] = None
    outside_encouragement: Optional[List[str]] = None
    opportunity_to_transfer: Optional[str] = None
    current_gpa: Optional[str] = None
    academic_difficulty: Optional[str] = None
    satisfaction: Optional[str] = None

class UniversityModel(BaseModel):
   name: str
   short_hand: str
   website: Optional[str] = None
   data_files: Optional[List[str]] = None
   include_majors: Optional[List[str]] = None
   exclude_majors: Optional[List[str]] = None
   categorize_by_school: Optional[List[str]] = None

class UserSignIn(BaseModel):
    email: EmailStr
    password: str

class TokenVerification(BaseModel):
    token: str

class RecommendationRequest(BaseModel):
    user_email: Optional[EmailStr] = None  # Optional, can be extracted from token
    category: str = "orgs"  # orgs, events, tutoring

class ScholarshipRequest(BaseModel):
    user_email: EmailStr
    scholarships_data: List[Dict[str, Any]]

class ChatGPTMessageModel(BaseModel):
    role: str
    content: str

class ChatGPTRequest(BaseModel):
    user_email: EmailStr
    messages: List[ChatGPTMessageModel]
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int = 150
    stream: bool = False

class ChatGPTResponse(BaseModel):
    user_email: EmailStr
    message: str
    timestamp: str
    conversation_id: str

class CalendarRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    categories: Optional[List[str]] = None
    location: Optional[str] = None

# ----------------------------
# OpenAI client portion (kept from original)
# ----------------------------
# WebSocket connection management
RATE_LIMIT_MESSAGES = 20  # Max messages per minute
RATE_LIMIT_WINDOW = 60  # 1 minute in seconds

class WebSocketConnection:
    def __init__(self, websocket: WebSocket, user_email: str):
        self.websocket = websocket
        self.user_email = user_email
        self.message_count = 0
        self.last_reset = time.time()
        self.conversation_history = []
        self.last_sent_payload: Optional[str] = None
        
    def is_rate_limited(self) -> bool:
        current_time = time.time()
        # Reset counter if window has passed
        if current_time - self.last_reset > RATE_LIMIT_WINDOW:
            self.message_count = 0
            self.last_reset = current_time
        
        self.message_count += 1
        return self.message_count > RATE_LIMIT_MESSAGES

class ChatMessage(BaseModel):
    message: str
    type: str = "chat"
    conversation_id: Optional[str] = None

class ChatGPTConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocketConnection] = {}

    async def connect(self, websocket: WebSocket, user_email: str):
        await websocket.accept()
        connection = WebSocketConnection(websocket, user_email)
        self.active_connections[user_email] = connection
        logger.info(f"🔌 Connected: {user_email}")
        return connection

    def disconnect(self, user_email: str):
        if user_email in self.active_connections:
            del self.active_connections[user_email]
            logger.info(f"🔌 Disconnected: {user_email}")

    async def send_personal_message(self, message: str, user_email: str):
        connection = self.active_connections.get(user_email)
        if connection:
            if connection.last_sent_payload == message:
                logger.debug(
                    "Skipping duplicate message for %s: %s",
                    user_email,
                    message[:100],
                )
                return
            await connection.websocket.send_text(message)
            connection.last_sent_payload = message

    def get_connection(self, user_email: str) -> Optional[WebSocketConnection]:
        return self.active_connections.get(user_email)

# Initialize connection manager
chatgpt_manager = ChatGPTConnectionManager()

# ----------------------------
# Legacy fallback: load local CSV/XLSX files (kept from original)
# We'll use these as fallback if university data isn't present.
# ----------------------------
def load_local_fallbacks():
    """
    Attempt to load original local files into memory for fallback.
    This will be used if university data map does not provide a file.
    """
    fallbacks = {}
    try:
        if os.path.exists("data/CC_activities_ex.csv"):
            df = pd.read_csv("data/CC_activities_ex.csv")
            if 'List of Interests' in df.columns:
                df['List of Interests'] = df['List of Interests'].apply(ast.literal_eval)
            fallbacks['activities_df'] = df.to_dict(orient="records")
        if os.path.exists("data/organizations_with_specific_majors.csv"):
            df = pd.read_csv("data/organizations_with_specific_majors.csv")
            fallbacks['orgs_df'] = df.to_dict(orient="records")
        if os.path.exists("data/filtered_utd_events_with_categories.csv"):
            df = pd.read_csv("data/filtered_utd_events_with_categories.csv")
            fallbacks['events_df'] = df.to_dict(orient="records")
        if os.path.exists("data/utd_courses.csv"):
            df = pd.read_csv("data/utd_courses.csv")
            fallbacks['courses_df'] = df.to_dict(orient="records")
        if os.path.exists("data/UTD_tutoring.xlsx"):
            try:
                tdf = pd.read_excel("data/UTD_tutoring.xlsx", engine="openpyxl")
                fallbacks['tutoring_df'] = tdf.to_dict(orient="records")
            except Exception:
                pass
        if os.path.exists("data/utd_events.csv"):
            df = pd.read_csv("data/utd_events.csv")
            fallbacks['calendar_df'] = df.to_dict(orient="records")
    except Exception as e:
        logger.exception("Error loading local fallback files")
    return fallbacks

_local_fallbacks = load_local_fallbacks()

# ----------------------------
# Utility helpers from original (kept)
# ----------------------------
def format_datetime(iso_string):
    if not isinstance(iso_string, str) or not iso_string.strip():
        return "Time Not Found"
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%A, %B %d, %Y, %I:%M %p")
    except ValueError:
        return iso_string

def extract_event_name(url):
    path = urlparse(url).path
    event_name = path.split("/event/")[-1] if "/event/" in path else ""
    event_name = event_name.replace("-", " ").title()
    return event_name

def get_college_by_major(major):
    utd_majors = {
        "School of Arts, Humanities, and Technology": ["Arts", "Humanities", "Technology"],
        "Naveen Jindal School of Management": ["Business", "Management", "Finance", "Accounting"],
        "Erik Jonsson School of Engineering and Computer Science": ["Computer Science", "Software Engineering", "Computer Engineering", "Electrical Engineering"],
        "School of Natural Sciences and Mathematics": ["Mathematics", "Physics", "Chemistry", "Biology"],
        "School of Behavioral and Brain Sciences": ["Psychology", "Neuroscience", "Cognitive Science"],
        "School of Economic, Political and Policy Sciences": ["Economics", "Political Science", "Public Policy"]
    }
    for college, majors_list in utd_majors.items():
        if major in majors_list:
            return college
    return "Any College"

# ----------------------------
# API Routes (original preserved; modified to use load_university_data when applicable)
# ----------------------------
@app.get("/")
async def root():
    return {"message": "Campus Connect API", "version": "1.0.0"}

@app.get("/health")
async def health_check():
    """Health check endpoint for load balancers and monitoring"""
    try:
        # Quick check if Firebase is initialized
        db = get_db()
        if db is None:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": "Database not initialized"}
            )
        return {"status": "healthy", "version": "1.0.0"}
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": str(e)}
        )


# Majors endpoint: read from Firestore directly when possible (original preserved)
@app.get("/majors")
async def get_majors(current_user: Dict[str, Any] = Depends(get_current_user)):
    try:
        database = get_db()
        if not database:
            return {"majors": [], "error": "Database not available"}
        majors_doc = database.collection("majors").document("all_majors").get()
        if majors_doc.exists:
            majors_data = majors_doc.to_dict()
            majors_list = majors_data.get("majors", [])
            return {"majors": majors_list}
        else:
            return {"majors": [], "error": "Document not found"}
    except Exception as e:
        return {"majors": [], "error": str(e)}

@app.get("/categories")
async def get_categories(current_user: Dict[str, Any] = Depends(get_current_user)):
    # For compatibility, try to return categories from fallback local file if available
    options = []
    try:
        if 'orgs_df' in _local_fallbacks:
            df_records = _local_fallbacks['orgs_df']
            # attempt extract categories
            cats = []
            for r in df_records:
                cat = r.get('Category')
                if cat:
                    cleaned = re.sub(r'[\[\]"]', '', str(cat)).strip()
                    cats.extend([item.strip() for item in re.split(r'[;,\.]', cleaned)])
            options = sorted(set(cats))
    except Exception:
        pass
    return {"categories": options}

@app.get("/major-colors")
async def get_major_colors(current_user: Dict[str, Any] = Depends(get_current_user)):
    # try Firestore
    database = get_db()
    if not database:
        return {"major_colors": {}}
    try:
        majors_doc = database.collection("majors").document("all_majors").get()
        if majors_doc.exists:
            return {"major_colors": majors_doc.to_dict().get("major_colors", {})}
    except Exception:
        pass
    return {"major_colors": {}}

# ORGANIZATIONS endpoint - preserved path & method; now university-aware
# ----------------------------
# Safe JSON serialization helper
# ----------------------------
def sanitize_for_json(obj: Any) -> Any:
    """
    Recursively sanitize data structure to prevent JSON serialization errors.
    Replaces NaN, inf, and -inf with None.
    """
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(item) for item in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif pd.isna(obj):
        return None
    return obj

# ----------------------------
# API Root endpoint
# ----------------------------
@app.get("/")
async def root():
    """API information endpoint"""
    return {
        "message": "Campus Connect API",
        "version": "1.0.0",
        "status": "running"
    }


# ----------------------------
# /organizations endpoint - Fetch from Firestore
# ----------------------------
@app.get("/organizations")
async def get_organizations(
    limit: int = 100,
    offset: int = 0,
    category: Optional[str] = None
):
    """
    Get organizations from Firestore - Fast and efficient.
    No authentication required.
    
    Args:
        limit: Maximum number of organizations to return (default 100)
        offset: Number of organizations to skip (default 0)
        category: Filter by category (optional)
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        logger.info(f"Fetching organizations from Firestore (limit={limit}, offset={offset}, category={category})")
        
        # Default university (can be made dynamic based on user)
        short_hand = "utd"
        
        # Build query
        query = db.collection('universities').document(short_hand).collection('organizations')
        
        # Apply category filter if provided
        if category:
            query = query.where('Category', '==', category)
        
        # Apply pagination
        query = query.limit(limit).offset(offset)
        
        # Execute query
        docs = query.stream()
        organizations = [doc.to_dict() for doc in docs]
        
        # Sanitize for JSON serialization
        organizations = sanitize_for_json(organizations)
        
        logger.info(f"Successfully retrieved {len(organizations)} organizations from Firestore")
        return {
            "organizations": organizations,
            "count": len(organizations),
            "source": "firestore"
        }
        
    except Exception as e:
        logger.exception(f"Error retrieving organizations from Firestore: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving organizations: {str(e)}")

# Signup / Signin preserved
@app.post("/signup")
@app.post("/auth/signup")
async def signup(user_data: UserProfile):
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    try:
        user_uid = user_data.uid
        user_email = user_data.email
        user_dict = user_data.dict()
        
        # Save user with UID as document ID
        db.collection('users').document(user_uid).set(user_dict)
        
        logger.info(f"User created successfully: {user_email} (UID: {user_uid})")
        
        return {
            "message": "User created successfully",
            "uid": user_uid,
            "email": user_email
        }
    except Exception as e:
        logger.error(f"Error creating user: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error creating user: {str(e)}")

@app.post("/signin")
async def signin(user_data: UserSignIn):
    database = get_db()
    if not database:
        raise HTTPException(status_code=500, detail="Database not available")
    try:
        user_email = user_data.email
        password = user_data.password
        
        # Get Firebase ID token using Firebase Auth REST API
        firebase_api_key = os.getenv("FIREBASE_API_KEY")
        logger.info(f"Using Firebase API key: {firebase_api_key[:20]}...")
        
        auth_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={firebase_api_key}"
        auth_payload = {
            "email": user_email,
            "password": password,
            "returnSecureToken": True
        }
        
        try:
            logger.info(f"Attempting Firebase auth for: {user_email}")
            auth_response = requests.post(auth_url, json=auth_payload)
            logger.info(f"Firebase auth response status: {auth_response.status_code}")
            
            if auth_response.status_code != 200:
                logger.error(f"Firebase auth error: {auth_response.text}")
            
            auth_response.raise_for_status()
            auth_data = auth_response.json()
            
            if "idToken" in auth_data and "localId" in auth_data:
                # Get user UID from Firebase Auth response
                user_uid = auth_data["localId"]
                logger.info(f"Firebase auth successful for UID: {user_uid}")
                
                # Get user document from Firestore using UID
                user_doc = database.collection('users').document(user_uid).get()
                
                if not user_doc.exists:
                    logger.warning(f"User authenticated but not found in Firestore: {user_uid}")
                    # User authenticated in Firebase but not in our database
                    # Return auth tokens but no user data
                    return {
                        "message": "Sign-in successful but profile incomplete",
                        "user": None,
                        "uid": user_uid,
                        "idToken": auth_data["idToken"],
                        "refreshToken": auth_data["refreshToken"],
                        "expiresIn": auth_data["expiresIn"]
                    }
                
                return {
                    "message": "Sign-in successful",
                    "user": user_doc.to_dict(),
                    "uid": user_uid,
                    "idToken": auth_data["idToken"],
                    "refreshToken": auth_data["refreshToken"],
                    "expiresIn": auth_data["expiresIn"]
                }
            else:
                raise HTTPException(status_code=401, detail="Invalid credentials")
                
        except requests.exceptions.RequestException:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        except Exception:
            raise HTTPException(status_code=401, detail="Authentication failed")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during sign-in: {str(e)}")

@app.post("/verify-token")
async def verify_token(token_data: TokenVerification):
    try:
        decoded_token = auth.verify_id_token(token_data.token)
        user_email = decoded_token.get("email")
        return {"user": user_email}
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

@app.post("/get-custom-token")
async def get_custom_token(user_data: UserSignIn):
    """Create a custom token for testing - bypasses password check"""
    try:
        # Create custom token using Firebase Admin SDK
        custom_token = auth.create_custom_token(user_data.email)
        return {
            "customToken": custom_token.decode('utf-8'),
            "message": "Use this token to get an ID token"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating custom token: {str(e)}")

@app.get("/profile/{user_id}")
async def get_profile(user_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """Get user profile with authentication - user_id can be UID or email for backward compatibility"""
    database = get_db()
    if not database:
        raise HTTPException(status_code=500, detail="Database not available")
    try:
        logger.info(f"Fetching profile for user: {user_id}")
        
        # Try to get by UID first (new method)
        user_doc = database.collection('users').document(user_id).get()
        
        # If not found and looks like email, try querying by email field (backward compatibility)
        if not user_doc.exists and '@' in user_id:
            logger.info(f"User not found by UID, trying email query: {user_id}")
            users_query = database.collection('users').where('email', '==', user_id).limit(1).stream()
            for doc in users_query:
                user_doc = doc
                break
        
        if user_doc.exists:
            user_data = user_doc.to_dict()
            logger.info(f"User data found: name={user_data.get('name')}, surname={user_data.get('surname')}, uid={user_data.get('uid')}")
            return {"user": user_data}
        else:
            logger.warning(f"User not found: {user_id}")
            raise HTTPException(status_code=404, detail="User not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving profile: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error retrieving profile: {str(e)}")

@app.put("/profile/{user_id}")
async def update_profile(user_id: str, user_data: UserProfile, current_user: Dict[str, Any] = Depends(get_current_user)):
    """Update user profile - user_id should be UID"""
    database = get_db()
    if not database:
        raise HTTPException(status_code=500, detail="Database not available")
    try:
        user_dict = user_data.dict()
        
        # Use UID from the user_data if provided, otherwise use path parameter
        uid_to_update = user_data.uid if user_data.uid else user_id
        
        logger.info(f"Updating profile for UID: {uid_to_update}")
        database.collection('users').document(uid_to_update).update(user_dict)
        
        return {
            "message": "Profile updated successfully",
            "uid": uid_to_update
        }
    except Exception as e:
        logger.error(f"Error updating profile: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating profile: {str(e)}")

# ----------------------------
# Recommendation scoring helpers
# ----------------------------
def get_college_by_major(major: str) -> str:
    """Map major to college/school name."""
    utd_majors = {
        "School of Arts, Humanities, and Technology": ["Arts", "Humanities", "Technology"],
        "Naveen Jindal School of Management": ["Business", "Management", "Finance", "Accounting"],
        "Erik Jonsson School of Engineering and Computer Science": ["Computer Science", "Software Engineering", "Computer Engineering", "Electrical Engineering"],
        "School of Natural Sciences and Mathematics": ["Mathematics", "Physics", "Chemistry", "Biology"],
        "School of Behavioral and Brain Sciences": ["Psychology", "Neuroscience", "Cognitive Science"],
        "School of Economic, Political and Policy Sciences": ["Economics", "Political Science", "Public Policy"]
    }
    for college, majors_list in utd_majors.items():
        if major in majors_list:
            return college
    return "Any College"

def calculate_support_ratings(user_data: Dict[str, Any]) -> Dict[str, float]:
    """Calculate social, intellectual, and career development support ratings."""
    academic_difficulty = user_data.get('academic_difficulty', 'Moderate')
    stress_level = user_data.get('stress_level', 'Low')
    gpa_range = user_data.get('gpa_range', '<2.0')
    self_efficacy = user_data.get('self_efficacy', 'Moderate')
    satisfaction = user_data.get('satisfaction', 'Neutral')
    financial_factors = user_data.get('financial_factors', 'N/A')
    family_responsibilities = user_data.get('family_responsibilities', 'N/A')
    outside_encouragement = user_data.get('outside_encouragement', []) or []
    
    # Social support rating
    social_score = 0
    if academic_difficulty == "Difficult" or stress_level == "High":
        social_score += 2
    if "Peers" in outside_encouragement or "Community" in outside_encouragement:
        social_score -= 1
    if satisfaction == "Dissatisfied":
        social_score += 2
    social_support = min(max(social_score, 1), 5)
    
    # Intellectual support rating
    intellectual_score = 0
    if gpa_range in ["< 2.0", "2.0 - 2.5"]:
        intellectual_score += 3
    if academic_difficulty == "Difficult":
        intellectual_score += 2
    if self_efficacy == "Little Belief":
        intellectual_score += 2
    if "Teachers" in outside_encouragement:
        intellectual_score -= 1
    intellectual_support = min(max(intellectual_score, 1), 5)
    
    # Career development rating
    career_score = 0
    if financial_factors in ["Work Income", "Loan"]:
        career_score += 1
    if satisfaction == "Neutral" or self_efficacy == "Some Belief":
        career_score += 1
    if family_responsibilities == "High":
        career_score += 1
    if "Family" in outside_encouragement:
        career_score -= 1
    career_development = min(max(career_score, 1), 5)
    
    return {
        'social_support': float(social_support),
        'intellectual_support': float(intellectual_support),
        'career_development': float(career_development)
    }

def score_organization(org: Dict[str, Any], user_data: Dict[str, Any], ratings: Dict[str, float]) -> Dict[str, Any]:
    """Score a single organization based on user profile."""
    score = 0.0
    explanation_parts = []
    
    year = user_data.get('year', '1')
    major = user_data.get('major', 'Undeclared')
    interests = user_data.get('interests', []) or []
    ftcs_status = user_data.get('ftcs_status', 'No')
    
    # Get organization attributes
    category = org.get('Category') or org.get('category') or ''
    org_majors = org.get('Majors') or org.get('majors') or ''
    specific_majors = org.get('Specific Majors') or org.get('specific_majors') or []
    if isinstance(specific_majors, str):
        try:
            specific_majors = ast.literal_eval(specific_majors)
        except:
            specific_majors = [specific_majors] if specific_majors else []
    
    user_college = get_college_by_major(major)
    
    # Scoring logic
    # First-year students and social categories
    if year == "1" and category in ["Cultural", "Social", "Recreation"]:
        score += ratings['social_support'] / 3.0
        explanation_parts.append("This activity is ideal for first-year students to connect socially.")
    
    # Upper-year students and academic categories
    elif year in ["3", "4", "5+"] and category in ["Academic Interests", "Educational/Departmental"]:
        score += 1.0
        explanation_parts.append("This activity provides valuable educational experience for upper-year students.")
    
    # Interest matching
    if interests and any(interest.lower() in category.lower() for interest in interests):
        score += 1.0
        explanation_parts.append("This activity aligns with your interests.")
    
    # College relevance
    if org_majors == user_college:
        score += 2.0
        explanation_parts.append("This activity is relevant to your college.")
    elif org_majors == 'any major' or org_majors == 'Any Major':
        score += 0.5
        explanation_parts.append("This activity is open to all majors.")
    
    # Major-specific alignment
    if major in specific_majors:
        score += 3.0
        explanation_parts.append("This activity directly aligns with your major.")
    
    # FTC status bonus
    if ftcs_status and ftcs_status != "No":
        score += 0.5
        explanation_parts.append("Great for first-generation college students.")
    
    # Create scored organization
    scored_org = dict(org)
    scored_org['Score'] = round(score, 2)
    scored_org['Recommendation Explanation'] = " ".join(explanation_parts) if explanation_parts else "This is a good match for you."
    
    return scored_org

# ----------------------------
# /recommendations endpoint - Fetch from Firestore with scoring
# ----------------------------
@app.post("/recommendations")
async def get_recommendations(
    request: RecommendationRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    limit: int = 7,
    offset: int = 0
):
    # Core logic
    database = get_db()
    if not database:
        raise HTTPException(status_code=500, detail="Database not available")

    try:
        # Extract Firebase UID and email
        firebase_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
        user_email = current_user.get("email") or request.user_email
        
        # Fetch user data from Firestore 'users' collection
        logger.info(f"Fetching user data from Firestore for: {firebase_uid} (email: {user_email})")
        user_doc = database.collection('users').document(firebase_uid).get()
        if not user_doc.exists:
            logger.warning(f"User not found in Firestore: {firebase_uid}")
            raise HTTPException(status_code=404, detail="User not found")

        user_data = user_doc.to_dict()
        logger.info(f"User data retrieved successfully for: {firebase_uid} (email: {user_email})")
        
        # Calculate support ratings
        ratings = calculate_support_ratings(user_data)
        
        # Determine category (default to 'orgs')
        category = request.category or "orgs"
        logger.info(f"Generating recommendations for category: {category}")
        
        # Get user's university
        short_hand = user_data.get('short_hand') or user_data.get('university') or 'utd'
        
        # Fetch organizations from Firestore university-specific collection
        if category == "orgs":
            logger.info(f"Fetching organizations from Firestore for {short_hand}")
            orgs_ref = database.collection('universities').document(short_hand).collection('organizations')
            orgs_docs = orgs_ref.stream()
            
            organizations = []
            for doc in orgs_docs:
                try:
                    org_data = doc.to_dict()
                    if 'id' not in org_data:
                        org_data['id'] = doc.id
                    organizations.append(org_data)
                except Exception as e:
                    logger.warning(f"Error processing organization document {doc.id}: {e}")
                    continue
            
            # Score organizations
            scored_orgs = []
            for org in organizations:
                try:
                    scored_org = score_organization(org, user_data, ratings)
                    scored_orgs.append(scored_org)
                except Exception as e:
                    logger.warning(f"Error scoring organization {org.get('id', 'unknown')}: {e}")
                    continue
            
            # Sort by score and apply limit/offset
            scored_orgs.sort(key=lambda x: x.get('Score', 0), reverse=True)
            results = scored_orgs[offset:offset + limit]
            
        elif category == "events":
            logger.info(f"Fetching events from Firestore for {short_hand}")
            events_ref = database.collection('universities').document(short_hand).collection('events')
            events_docs = events_ref.limit(50).stream()
            
            events = [doc.to_dict() for doc in events_docs]
            # Simple scoring for events (can be enhanced later)
            for event in events:
                event['Score'] = 1.0
                event['Recommendation Explanation'] = "Recommended event for you"
            
            results = events[:limit]
            
        elif category == "tutoring":
            logger.info(f"Fetching tutoring from Firestore for {short_hand}")
            tutoring_ref = database.collection('universities').document(short_hand).collection('tutoring')
            tutoring_docs = tutoring_ref.limit(50).stream()
            
            tutoring = [doc.to_dict() for doc in tutoring_docs]
            # Simple scoring for tutoring (can be enhanced later)
            for item in tutoring:
                item['Score'] = 1.0
                item['Recommendation Explanation'] = "Recommended tutoring resource for you"
            
            results = tutoring[:limit]
            
        else:
            logger.warning(f"Unknown category: {category}")
            results = []

        # Sanitize results to prevent NaN/inf serialization errors
        results = sanitize_for_json(results)
        
        logger.info(f"Returning {len(results)} recommendations for category: {category}")
        return {
            "recommendations": results,
            "category": category,
            "count": len(results)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error generating recommendations: {e}")
        raise HTTPException(status_code=500, detail=f"Error generating recommendations: {str(e)}")

# ----------------------------
# Additional endpoints for frontend compatibility
# ----------------------------
@app.post("/recommendations/events")
async def get_event_recommendations(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Get event recommendations - frontend compatibility endpoint"""
    try:
        logger.info(f"Event recommendations requested by user: {current_user}")
        
        # Extract user email from current_user
        user_email = current_user.get("email")
        if not user_email:
            raise HTTPException(status_code=400, detail="User email not found in authentication token")
        logger.info(f"Using user email: {user_email}")
        
        # For events, just return empty results since they're not implemented yet
        logger.info("Returning empty events results (not implemented)")
        return {
            "recommendations": [],
            "category": "events",
            "count": 0,
            "message": "Events recommendations not yet implemented"
        }
        
    except Exception as e:
        logger.exception(f"Error in event recommendations: {e}")
        logger.error(f"Exception type: {type(e)}")
        logger.error(f"Exception args: {e.args}")
        raise HTTPException(status_code=500, detail=f"Error getting event recommendations: {str(e)}")

@app.post("/recommendations/organizations")
async def get_organization_recommendations(
    current_user: Dict[str, Any] = Depends(get_current_user),
    limit: int = 10,
    offset: int = 0
):
    """Get organization recommendations from Firestore - frontend compatibility endpoint"""
    database = get_db()
    if not database:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        logger.info(f"Organization recommendations requested by user: {current_user}")
        
        # Get user UID and email
        firebase_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
        user_email = current_user.get("email")
        if not firebase_uid:
            raise HTTPException(status_code=400, detail="User UID not found in authentication token")
        
        # Get user data and short_hand
        user_doc = database.collection('users').document(firebase_uid).get()
        if not user_doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_data = user_doc.to_dict()
        short_hand = user_data.get("short_hand") or user_data.get("university") or "utd"
        logger.info(f"Fetching organizations from Firestore for {short_hand}")
        
        # Calculate support ratings for scoring
        ratings = calculate_support_ratings(user_data)
        
        # Query organizations from Firestore
        orgs_ref = database.collection('universities').document(short_hand).collection('organizations')
        orgs_docs = orgs_ref.stream()
        
        organizations = []
        for doc in orgs_docs:
            try:
                org_data = doc.to_dict()
                if 'id' not in org_data:
                    org_data['id'] = doc.id
                organizations.append(org_data)
            except Exception as e:
                logger.warning(f"Error processing organization document {doc.id}: {e}")
                continue
        
        # Score organizations
        scored_orgs = []
        for org in organizations:
            try:
                scored_org = score_organization(org, user_data, ratings)
                scored_orgs.append(scored_org)
            except Exception as e:
                logger.warning(f"Error scoring organization {org.get('id', 'unknown')}: {e}")
                continue
        
        # Sort by score and apply limit/offset
        scored_orgs.sort(key=lambda x: x.get('Score', 0), reverse=True)
        results = scored_orgs[offset:offset + limit]
        
        # Sanitize for JSON serialization
        organizations = sanitize_for_json(organizations)
        
        # Apply limit and offset
        total_count = len(organizations)
        organizations = organizations[offset:offset + limit]
        
        logger.info(f"Returning {len(organizations)} organization recommendations (total: {total_count})")
        return {
            "recommendations": organizations,
            "category": "orgs",
            "count": len(organizations),
            "total": total_count
        }
        
    except Exception as e:
        logger.exception(f"Error in organization recommendations: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting organization recommendations: {str(e)}")

@app.get("/calendar-events")
async def get_calendar_events(
    limit: int = 100,
    offset: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    """Get calendar events from Firestore - Fast and efficient"""
    start_time = time.time()
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        short_hand = "utd"  # Default university
        logger.info(f"Fetching calendar events from Firestore for {short_hand}")
        
        # Build query
        query = db.collection('universities').document(short_hand).collection('events')
        
        # Apply date filters if provided
        if start_date:
            query = query.where('date', '>=', start_date)
        if end_date:
            query = query.where('date', '<=', end_date)
        
        # Order by date and apply pagination
        query = query.order_by('date').limit(limit).offset(offset)
        
        # Execute query
        docs = query.stream()
        events = [doc.to_dict() for doc in docs]
        
        # Sanitize for JSON
        events = sanitize_for_json(events)
        
        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(f"Calendar events request completed in {elapsed_ms:.2f}ms, retrieved {len(events)} events")
        
        return {
            "events": events,
            "count": len(events),
            "source": "firestore",
            "response_time_ms": elapsed_ms
        }
        
    except Exception as e:
        logger.exception(f"Error in calendar events: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving calendar events: {str(e)}")

def _get_today_events_cached(short_hand: str) -> List[Dict[str, Any]]:
    """Get today's events with caching to avoid repeated filtering and date parsing."""
    today = datetime.now().date()
    today_str = today.isoformat()
    now = time.time()
    
    # Check cache first
    with _today_events_cache_lock:
        cache_entry = _today_events_cache.get(short_hand)
        if cache_entry:
            # Check if cache is still valid and for today's date
            if (cache_entry.get("date") == today_str and 
                (now - cache_entry.get("loaded_at", 0)) < TODAY_EVENTS_CACHE_TTL):
                logger.debug(f"Cache hit for today's events: {short_hand}")
                return cache_entry["events"]
    
    # Cache miss or expired - rebuild
    logger.info(f"Rebuilding today's events cache for {short_hand}")
    
    # Load university data
    university_data = load_university_data(short_hand)
    
    # Look for calendar/events data
    all_events = (university_data.get("calendar_df") or 
                 university_data.get("events_df") or
                 university_data.get("activities_df") or
                 university_data.get("calendar") or 
                 university_data.get("events"))
    
    if all_events is None:
        all_events = _local_fallbacks.get("calendar_df", [])
        logger.info("Using fallback calendar data for today's events")
    
    # Filter for today's events using optimized helper
    today_events = []
    if isinstance(all_events, list):
        for event in all_events:
            if isinstance(event, dict):
                event_datetime = _parse_event_date(event)
                if event_datetime and event_datetime.date() == today:
                    today_events.append(event)
    
    logger.info(f"Found {len(today_events)} events for today out of {len(all_events) if isinstance(all_events, list) else 0} total events")
    
    # Sanitize once and cache
    today_events = sanitize_for_json(today_events)
    
    # Update cache
    with _today_events_cache_lock:
        _today_events_cache[short_hand] = {
            "date": today_str,
            "events": today_events,
            "loaded_at": now
        }
    
    return today_events

@app.get("/today-events")
async def get_today_events(
    limit: int = 10
):
    """Get today's events from Firestore - Fast and efficient"""
    start_time = time.time()
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        short_hand = "utd"  # Default university
        today = datetime.now().date().isoformat()
        logger.info(f"Fetching today's events from Firestore for {short_hand} (date={today})")
        
        # Query events for today
        query = db.collection('universities').document(short_hand).collection('events')
        query = query.where('date', '>=', today).where('date', '<', today + 'T23:59:59')
        query = query.order_by('date').limit(limit)
        
        # Execute query
        docs = query.stream()
        events = [doc.to_dict() for doc in docs]
        
        # Sanitize for JSON
        events = sanitize_for_json(events)
        
        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(f"Today events request completed in {elapsed_ms:.2f}ms, retrieved {len(events)} events")
        
        return {
            "events": events,
            "count": len(events),
            "date": today,
            "source": "firestore",
            "response_time_ms": elapsed_ms
        }
        
    except Exception as e:
        logger.exception(f"Error in today's events: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving today's events: {str(e)}")

# PERSONALIZED SCHOLARSHIPS preserved (POST) - uses OpenAI as original if available
@app.post("/personalized-scholarships")
async def get_personalized_scholarships_endpoint(request: ScholarshipRequest):
    if not get_db():
        raise HTTPException(status_code=500, detail="Database not available")
    if not client:
        raise HTTPException(status_code=500, detail="OpenAI service not available")
    try:
        user_doc = get_db().collection('users').document(request.user_email).get()
        if not user_doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        user_data = user_doc.to_dict()

        # Create prompt similar to original and use client
        prompt = f"""
        Given a student with this profile: {json.dumps(user_data, indent=2)}
        Evaluate scholarships and return most relevant ones in JSON format.
        Scholarships: {json.dumps(request.scholarships_data, indent=2)}
        """

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a scholarship matching expert."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=1500
        )
        # parse response
        try:
            recommendations = json.loads(response.choices[0].message.content)
        except Exception:
            recommendations = {"error": "Failed to parse OpenAI response", "raw": str(response)}
        return {"recommendations": recommendations}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error generating personalized scholarships")
        raise HTTPException(status_code=500, detail=f"Error generating personalized scholarships: {str(e)}")

# UNIVERSITY CRUD endpoints (preserved from original and rewrite compatibility)
@app.post("/universities")
def create_universty(univ: UniversityModel):
    try:
        db_instance = get_db()
        if db_instance is None:
            raise HTTPException(status_code=500, detail="Failed to initialize database connection")
        doc_ref = db_instance.collection("university").document(univ.short_hand.lower())
        if doc_ref.get().exists:
            raise HTTPException(status_code=400, detail="University already exists")
        doc_ref.set(univ.dict())
        return {"message": "University created successfully", "id": univ.short_hand.lower()}
    except Exception as e:
        logger.exception("Error creating university")
        raise HTTPException(status_code=500, detail=f"Failed to create university: {str(e)}")

@app.get("/universities/{short_hand}")
def get_university(short_hand: str):
    try:
        db_instance = get_db()
        if db_instance is None:
            raise HTTPException(status_code=500, detail="Failed to initialize database connection")
        doc_ref = db_instance.collection("university").document(short_hand.lower())
        doc = doc_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="University not found")
        return doc.to_dict()
    except Exception as e:
        logger.exception("Error fetching university")
        raise HTTPException(status_code=500, detail=f"Failed to fetch university: {str(e)}")

@app.put("/universities/{short_hand}")
def update_university(short_hand: str, univ: UniversityModel):
    try:
        db_instance = get_db()
        if db_instance is None:
            raise HTTPException(status_code=500, detail="Failed to initialize database connection")
        doc_ref = db_instance.collection("university").document(short_hand.lower())
        doc = doc_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="University not found")
        update_data = {k: v for k, v in univ.dict().items() if v is not None}
        doc_ref.update(update_data)
        # clear cache for this university
        with _university_cache_lock:
            if short_hand.lower() in _university_cache:
                del _university_cache[short_hand.lower()]
        return {"message": "University updated successfully"}
    except Exception as e:
        logger.exception("Error updating university")
        raise HTTPException(status_code=500, detail=f"Failed to update university: {str(e)}")

@app.delete("/universities/{short_hand}")
def delete_university(short_hand: str):
    try:
        db_instance = get_db()
        if db_instance is None:
            raise HTTPException(status_code=500, detail="Failed to initialize database connection")
        doc_ref = db_instance.collection("university").document(short_hand.lower())
        doc = doc_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="University not found")
        doc_ref.delete()
        with _university_cache_lock:
            if short_hand.lower() in _university_cache:
                del _university_cache[short_hand.lower()]
        return {"message": "University deleted successfully"}
    except Exception as e:
        logger.exception("Error deleting university")
        raise HTTPException(status_code=500, detail=f"Failed to delete university: {str(e)}")

@app.get("/universities")
def list_universities():
    try:
        db_instance = get_db()
        if db_instance is None:
            raise HTTPException(status_code=500, detail="Failed to initialize database connection")
        universities_ref = db_instance.collection("university")
        universities = universities_ref.stream()
        result = []
        for university in universities:
            try:
                result.append(university.to_dict())
            except Exception:
                continue
        return result
    except Exception as e:
        logger.exception("Error listing universities")
        raise HTTPException(status_code=500, detail=f"Failed to list universities: {str(e)}")

# CONTENT endpoints (university-scoped)
def _get_short_hand_for_current_user(decoded_token: Dict[str, Any]) -> str:
    uid = decoded_token.get("uid") or decoded_token.get("user_id") or decoded_token.get("sub")
    if not uid:
        uid = decoded_token.get("email")
    if not uid:
        raise HTTPException(status_code=400, detail="Cannot determine user id from token")
    short_hand = get_user_short_hand_from_firestore(uid)
    if not short_hand:
        raise HTTPException(status_code=404, detail="User's university short_hand not set in Firestore")
    return short_hand

@app.get("/events")
async def get_events(
    current_user: Dict[str, Any] = Depends(get_current_user),
    limit: int = 100,
    offset: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    """Get events from Firestore with optional date filtering"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        short_hand = _get_short_hand_for_current_user(current_user)
        logger.info(f"Fetching events from Firestore for {short_hand}")
        
        # Build query
        query = db.collection('universities').document(short_hand).collection('events')
        
        # Apply date filters if provided
        if start_date:
            query = query.where('date', '>=', start_date)
        if end_date:
            query = query.where('date', '<=', end_date)
        
        # Order by date and apply pagination
        query = query.order_by('date').limit(limit).offset(offset)
        
        # Execute query
        docs = query.stream()
        events = [doc.to_dict() for doc in docs]
        
        # Sanitize for JSON
        events = sanitize_for_json(events)
        
        logger.info(f"Retrieved {len(events)} events from Firestore")
        return {
            "events": events,
            "count": len(events),
            "source": "firestore"
        }
        
    except Exception as e:
        logger.exception(f"Error retrieving events: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving events: {str(e)}")

# NOTE: /calendar preserved as POST (to avoid the 405 issue). Use CalendarRequest body if needed.
@app.post("/calendar")
async def post_calendar(request: Optional[CalendarRequest] = None):
    """
    POST /calendar preserved for compatibility.
    If the frontend expects POST, we keep POST and return calendar events for the user's university.
    """
    # For testing, use default short_hand
    short_hand = "utd"  # Default for testing
    try:
        # Load university data from data_files array
        university_data = load_university_data(short_hand)
        logger.info(f"Available data keys for calendar: {list(university_data.keys())}")
        
        # Look for calendar/events data using the correct data_files keys
        calendar = (university_data.get("calendar_df") or 
                   university_data.get("events_df") or
                   university_data.get("activities_df") or
                   university_data.get("calendar") or 
                   university_data.get("events"))
        
        if calendar is None:
            # Fallback to local data
            calendar = _local_fallbacks.get("calendar_df", [])
            logger.info("Using fallback calendar data")
        else:
            logger.info(f"Found {len(calendar) if isinstance(calendar, list) else 'unknown'} calendar items")
        
        # optionally filter by request.start_date / end_date if provided
        if request and isinstance(request, CalendarRequest):
            # naive filtering if calendar is list of dicts with 'Start Time' or 'start'
            if isinstance(calendar, list) and (request.start_date or request.end_date):
                out = []
                for ev in calendar:
                    start_val = ev.get('Start Time') or ev.get('start') or ev.get('start_time')
                    include = True
                    if start_val and request.start_date:
                        try:
                            ev_dt = datetime.fromisoformat(start_val)
                            sd = datetime.fromisoformat(request.start_date)
                            if ev_dt < sd:
                                include = False
                        except Exception:
                            pass
                    if include:
                        out.append(ev)
                calendar = out
        
        # Sanitize calendar data for JSON serialization
        calendar = sanitize_for_json(calendar)
        
        return {"calendar": calendar}
    except Exception as e:
        logger.exception(f"Error in calendar endpoint: {e}")
        return {"calendar": [], "error": str(e)}

@app.websocket("/ws/chatgpt")
async def websocket_chatgpt(websocket: WebSocket, user_email: str = None):
    """Production-ready WebSocket endpoint for ChatGPT functionality"""
    if not user_email:
        await websocket.close(code=1008, reason="Missing user_email parameter")
        return
    
    try:
        # Use connection manager to handle connection
        connection = await chatgpt_manager.connect(websocket, user_email)
        
        # Send welcome message
        await chatgpt_manager.send_personal_message(json.dumps({
            "type": "connection",
            "status": "connected",
            "message": "Hello! I'm a Campus Connect assistant. I'd be happy to help you with your studies.",
            "user_email": user_email,
            "timestamp": datetime.utcnow().isoformat()
        }), user_email)
        
        while True:
            try:
                # Receive message with timeout (reduced from 300s to 60s to prevent hanging connections)
                data = await asyncio.wait_for(websocket.receive_text(), timeout=60)
                
                # Rate limiting check
                if connection.is_rate_limited():
                    await chatgpt_manager.send_personal_message(json.dumps({
                        "type": "error",
                        "error": "rate_limit_exceeded",
                        "message": f"Rate limit exceeded. Maximum {RATE_LIMIT_MESSAGES} messages per minute.",
                        "timestamp": datetime.utcnow().isoformat()
                    }), user_email)
                    continue
                
                # Parse and validate message
                try:
                    message_data = json.loads(data)
                    chat_message = ChatMessage(**message_data)
                except (json.JSONDecodeError, ValidationError) as e:
                    await chatgpt_manager.send_personal_message(json.dumps({
                        "type": "error",
                        "error": "invalid_message",
                        "message": f"Invalid message format: {str(e)}",
                        "timestamp": datetime.utcnow().isoformat()
                    }), user_email)
                    continue
                
                logger.info(f"Message from {user_email}: {chat_message.message[:100]}...")
                
                # Add to conversation history
                connection.conversation_history.append({
                    "role": "user",
                    "content": chat_message.message,
                    "timestamp": datetime.utcnow().isoformat()
                })
                
                # Process with ChatGPT
                try:
                    logger.info(f"Processing CCAI message for {user_email}")
                    
                    # Try OpenAI API call with fallback
                    try:
                        import openai
                        logger.info("✅ OpenAI library imported successfully")
                        
                        openai_api_key = os.getenv("OPENAI_API_KEY")
                        if not openai_api_key:
                            response_text = "ChatGPT service is currently unavailable. OpenAI API key not found."
                            logger.warning("OpenAI API key not found")
                        else:
                            logger.info(f"Using OpenAI API key: {openai_api_key[:20]}...")
                            client = openai.OpenAI(api_key=openai_api_key)
                            
                            messages = [
                                {"role": "system", "content": "You are a helpful AI assistant for Campus Connect, a university platform. Help students with academic questions, campus information, and general assistance."}
                            ]
                            
                            # Add recent conversation history
                            recent_history = connection.conversation_history[-10:]
                            for msg in recent_history:
                                messages.append({
                                    "role": msg["role"],
                                    "content": msg["content"]
                                })
                            
                            logger.info(f"Calling OpenAI API with {len(messages)} messages")
                            
                            # Get prompt template ID from environment (for logging/tracking)
                            prompt_template_id = os.getenv("PROMPT_TEMPLATE_ID")
                            if prompt_template_id:
                                logger.info(f"Using prompt template ID: {prompt_template_id}")
                            
                            response = client.chat.completions.create(
                                model="gpt-4o-mini",
                                messages=messages,
                                max_tokens=500,
                                temperature=0.7,
                                timeout=30
                            )
                            
                            response_text = response.choices[0].message.content.strip()
                            logger.info(f"OpenAI response: {response_text[:100]}...")
                            
                    except ImportError as e:
                        logger.error(f"OpenAI library not available: {e}")
                        response_text = f"Hello! I'm a Campus Connect assistant. I'd be happy to help you with your studies, but AI integration is currently unavailable. You asked: '{chat_message.message}' "
                        
                    except Exception as openai_error:
                        logger.error(f"OpenAI API error: {type(openai_error).__name__}: {openai_error}")
                        response_text = f"I'm having trouble connecting to the AI right now. Please try again later."
                    
                    # Add response to history
                    connection.conversation_history.append({
                        "role": "assistant", 
                        "content": response_text,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    
                    # Send response
                    await chatgpt_manager.send_personal_message(json.dumps({
                        "type": "response",
                        "message": response_text,
                        "conversation_id": chat_message.conversation_id,
                        "timestamp": datetime.utcnow().isoformat()
                    }), user_email)
                    
                except Exception as e:
                    logger.error(f"CCAI Processing error for {user_email}: {type(e).__name__}: {e}")
                    logger.exception("Full CCAI processing error traceback:")
                    await chatgpt_manager.send_personal_message(json.dumps({
                        "type": "error",
                        "error": "processing_failed",
                        "message": f"Failed to process your message: {str(e)}",
                        "timestamp": datetime.utcnow().isoformat()
                    }), user_email)
                    
            except asyncio.TimeoutError:
                # Send ping to check connection
                try:
                    await chatgpt_manager.send_personal_message(json.dumps({
                        "type": "ping",
                        "timestamp": datetime.utcnow().isoformat()
                    }), user_email)
                except:
                    break
                    
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {user_email}")
    except Exception as e:
        logger.error(f"WebSocket error for {user_email}: {e}")
    finally:
        # Cleanup
        chatgpt_manager.disconnect(user_email)
        logger.info(f"WebSocket connection cleaned up: {user_email}")

@app.get("/ws/chatgpt/status")
async def get_websocket_status():
    """Get WebSocket connection status - HTTP endpoint for health checks"""
    return {
        "service": "ChatGPT WebSocket",
        "status": "running",
        "active_connections": len(chatgpt_manager.active_connections),
        "connections": [
            {
                "user_email": conn.user_email,
                "message_count": conn.message_count,
                "conversation_length": len(conn.conversation_history)
            }
            for user_email, conn in chatgpt_manager.active_connections.items()
        ],
        "rate_limit": {
            "messages_per_minute": RATE_LIMIT_MESSAGES,
            "window_seconds": RATE_LIMIT_WINDOW
        },
        "endpoint": "ws://localhost:8000/ws/chatgpt?user_email=YOUR_EMAIL"
    }

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Campus Connect API",
        "status": "running",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "websocket_chatgpt": "/ws/chatgpt?user_email=YOUR_EMAIL",
            "websocket_status": "/ws/chatgpt/status"
        }
    }

@app.get("/health")
async def health_check():
    """General health check endpoint - No authentication required"""
    return {
        "status": "healthy",
        "service": "Campus Connect API",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
        "database": "connected" if get_db() else "disconnected"
    }

@app.get("/health/websocket")
async def websocket_health_check():
    """Simple health check for WebSocket service"""
    return {
        "status": "healthy",
        "service": "ChatGPT WebSocket",
        "active_connections": len(chatgpt_manager.active_connections),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/scholarships")
async def get_scholarships(
    limit: int = 100,
    offset: int = 0,
    category: Optional[str] = None
):
    """Get scholarships from Firestore (no auth required for testing)"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        short_hand = "utd"  # Default university
        logger.info(f"Fetching scholarships from Firestore for {short_hand}")
        
        # Query scholarships collection
        query = db.collection('universities').document(short_hand).collection('scholarships')
        
        # Apply category filter if provided
        if category:
            query = query.where('category', '==', category)
        
        # Apply pagination
        query = query.limit(limit).offset(offset)
        
        # Execute query
        docs = query.stream()
        scholarships = [doc.to_dict() for doc in docs]
        
        # Sanitize for JSON
        scholarships = sanitize_for_json(scholarships)
        
        logger.info(f"Retrieved {len(scholarships)} scholarships from Firestore")
        return {
            "scholarships": scholarships,
            "count": len(scholarships),
            "source": "firestore"
        }
        
    except Exception as e:
        logger.exception(f"Error in scholarships endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving scholarships: {str(e)}")

@app.get("/orgs")
async def get_orgs(
    current_user: Dict[str, Any] = Depends(get_current_user),
    limit: int = 100,
    offset: int = 0
):
    """Get organizations from Firestore"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        short_hand = _get_short_hand_for_current_user(current_user)
        logger.info(f"Fetching organizations from Firestore for {short_hand}")
        
        # Query organizations
        query = db.collection('universities').document(short_hand).collection('organizations')
        query = query.limit(limit).offset(offset)
        
        docs = query.stream()
        orgs = [doc.to_dict() for doc in docs]
        orgs = sanitize_for_json(orgs)
        
        return {"orgs": orgs, "count": len(orgs), "source": "firestore"}
        
    except Exception as e:
        logger.exception(f"Error retrieving organizations: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving organizations: {str(e)}")

@app.get("/tutoring")
async def get_tutoring(
    current_user: Dict[str, Any] = Depends(get_current_user),
    limit: int = 100,
    offset: int = 0
):
    """Get tutoring resources from Firestore"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        short_hand = _get_short_hand_for_current_user(current_user)
        logger.info(f"Fetching tutoring from Firestore for {short_hand}")
        
        # Query tutoring
        query = db.collection('universities').document(short_hand).collection('tutoring')
        query = query.limit(limit).offset(offset)
        
        docs = query.stream()
        tutoring = [doc.to_dict() for doc in docs]
        tutoring = sanitize_for_json(tutoring)
        
        return {"tutoring": tutoring, "count": len(tutoring), "source": "firestore"}
        
    except Exception as e:
        logger.exception(f"Error retrieving tutoring: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving tutoring: {str(e)}")

@app.get("/courses")
async def get_courses(
    current_user: Dict[str, Any] = Depends(get_current_user),
    limit: int = 100,
    offset: int = 0
):
    """Get courses from Firestore"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        short_hand = _get_short_hand_for_current_user(current_user)
        logger.info(f"Fetching courses from Firestore for {short_hand}")
        
        # Query courses
        query = db.collection('universities').document(short_hand).collection('courses')
        query = query.limit(limit).offset(offset)
        
        docs = query.stream()
        courses = [doc.to_dict() for doc in docs]
        courses = sanitize_for_json(courses)
        
        return {"courses": courses, "count": len(courses), "source": "firestore"}
        
    except Exception as e:
        logger.exception(f"Error retrieving courses: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving courses: {str(e)}")

# ----------------------------
# User saved events endpoints (preserved)
# ----------------------------
@app.post("/users/me/saved")
async def save_personal_event(payload: Dict[str, Any], current_user: Dict[str, Any] = Depends(get_current_user)):
    """Save a personal/created event (not external events)"""
    event_id = payload.get("eventId") or payload.get("id")
    if not event_id:
        raise HTTPException(status_code=400, detail="eventId or id required")

    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to save events")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    # Save personal event data
    event_data = payload.copy()
    event_data["createdAt"] = firestore.SERVER_TIMESTAMP
    event_data["type"] = "personal"
    
    doc_ref = db.collection("users").document(user_uid).collection("events").document(str(event_id))
    doc_ref.set(event_data)
    return {"ok": True, "eventId": event_id, "type": "personal"}

@app.get("/users/me/saved")
async def get_personal_events(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Get only personal/created events (not external saved events)"""
    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to retrieve personal events")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    # Get personal/created events only
    events_ref = db.collection("users").document(user_uid).collection("events")
    events_docs = events_ref.stream()
    events = []
    for d in events_docs:
        event_data = d.to_dict()
        event_data["type"] = "personal"
        events.append(event_data)
    
    return {"events": events, "count": len(events)}

@app.delete("/users/me/saved/{event_id}")
async def delete_personal_event(event_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """Delete a personal/created event"""
    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to delete personal events")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    doc_ref = db.collection("users").document(user_uid).collection("events").document(event_id)
    doc_ref.delete()
    return {"ok": True, "eventId": event_id}

# ----------------------------
# User saved scholarships endpoints
# ----------------------------
@app.post("/users/me/scholarships")
async def save_scholarship(payload: Dict[str, Any], current_user: Dict[str, Any] = Depends(get_current_user)):
    scholarship_id = payload.get("scholarshipId") or payload.get("id")
    if not scholarship_id:
        raise HTTPException(status_code=400, detail="scholarshipId or id required")

    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to save scholarships")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    # Save all scholarship data for repopulation
    scholarship_data = payload.copy()
    scholarship_data["savedAt"] = firestore.SERVER_TIMESTAMP
    
    doc_ref = db.collection("users").document(user_uid).collection("scholarships").document(str(scholarship_id))
    doc_ref.set(scholarship_data)
    return {"ok": True, "scholarshipId": scholarship_id}

@app.get("/users/me/scholarships")
async def get_saved_scholarships(current_user: Dict[str, Any] = Depends(get_current_user)):
    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to retrieve saved scholarships")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    scholarships_ref = db.collection("users").document(user_uid).collection("scholarships")
    docs = scholarships_ref.stream()
    scholarships = []
    for d in docs:
        scholarships.append(d.to_dict())
    return {"scholarships": scholarships}

@app.delete("/users/me/scholarships/{scholarship_id}")
async def unsave_scholarship(scholarship_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """Delete a saved scholarship"""
    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to remove saved scholarships")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    doc_ref = db.collection("users").document(user_uid).collection("scholarships").document(scholarship_id)
    doc_ref.delete()
    return {"ok": True}

# ----------------------------
# User saved organizations endpoints
# ----------------------------
@app.post("/users/me/organizations")
async def save_organization(payload: Dict[str, Any], current_user: Dict[str, Any] = Depends(get_current_user)):
    """Save an organization"""
    organization_id = payload.get("organizationId") or payload.get("id")
    if not organization_id:
        raise HTTPException(status_code=400, detail="organizationId or id required")

    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to save organizations")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    # Save all organization data for repopulation
    organization_data = payload.copy()
    organization_data["savedAt"] = firestore.SERVER_TIMESTAMP
    
    doc_ref = db.collection("users").document(user_uid).collection("organizations").document(str(organization_id))
    doc_ref.set(organization_data)
    return {"ok": True, "organizationId": organization_id}

@app.get("/users/me/organizations")
async def get_saved_organizations(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Get all saved organizations"""
    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to retrieve saved organizations")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    organizations_ref = db.collection("users").document(user_uid).collection("organizations")
    docs = organizations_ref.stream()
    organizations = []
    for d in docs:
        organizations.append(d.to_dict())
    return {"organizations": organizations}

@app.delete("/users/me/organizations/{organization_id}")
async def unsave_organization(organization_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """Delete a saved organization"""
    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required to remove saved organizations")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    doc_ref = db.collection("users").document(user_uid).collection("organizations").document(organization_id)
    doc_ref.delete()
    return {"ok": True}

# ----------------------------
# Universal saved items DELETE endpoint
# ----------------------------
@app.delete("/users/me/saved-items/{item_id}")
async def delete_saved_item(
    item_id: str,
    type: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Delete a saved item by ID and type
    Supports: organization, scholarship, event
    """
    user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    if not user_uid:
        raise HTTPException(status_code=400, detail="Authenticated user UID required")

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    # Map type to collection name
    collection_map = {
        "organization": "organizations",
        "scholarship": "scholarships",
        "event": "events",
        "savedEvent": "savedEvents"
    }
    
    collection_name = collection_map.get(type)
    if not collection_name:
        raise HTTPException(status_code=400, detail=f"Invalid type: {type}. Must be one of: {list(collection_map.keys())}")
    
    doc_ref = db.collection("users").document(user_uid).collection(collection_name).document(item_id)
    doc_ref.delete()
    
    return {"ok": True, "itemId": item_id, "type": type}

# ----------------------------
# Get all saved items (events, scholarships, organizations, etc.)
# ----------------------------
async def _get_saved_items_logic(user_id: Optional[str], current_user: Optional[Dict[str, Any]]):
    """Shared logic for getting saved items"""
    # Get user_uid from either query param or authenticated user
    user_uid = None
    if user_id:
        user_uid = user_id
    elif current_user:
        user_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
    
    if not user_uid:
        raise HTTPException(
            status_code=400, 
            detail="user_id query parameter or authentication required"
        )

    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    result = {}
    
    # Get saved events
    saved_events_ref = db.collection("users").document(user_uid).collection("savedEvents")
    saved_events_docs = saved_events_ref.stream()
    saved_events = []
    for d in saved_events_docs:
        event_data = d.to_dict()
        event_data["type"] = "savedEvent"
        saved_events.append(event_data)
    result["savedEvents"] = saved_events
    
    # Get personal/created events
    events_ref = db.collection("users").document(user_uid).collection("events")
    events_docs = events_ref.stream()
    personal_events = []
    for d in events_docs:
        event_data = d.to_dict()
        event_data["type"] = "personalEvent"
        personal_events.append(event_data)
    result["events"] = personal_events
    
    # Get saved scholarships
    scholarships_ref = db.collection("users").document(user_uid).collection("scholarships")
    scholarships_docs = scholarships_ref.stream()
    scholarships = []
    for d in scholarships_docs:
        scholarship_data = d.to_dict()
        scholarship_data["type"] = "scholarship"
        scholarships.append(scholarship_data)
    result["scholarships"] = scholarships
    
    # Get saved organizations
    organizations_ref = db.collection("users").document(user_uid).collection("organizations")
    organizations_docs = organizations_ref.stream()
    organizations = []
    for d in organizations_docs:
        organization_data = d.to_dict()
        organization_data["type"] = "organization"
        organizations.append(organization_data)
    result["organizations"] = organizations
    
    # Count totals
    result["totals"] = {
        "savedEvents": len(saved_events),
        "events": len(personal_events),
        "scholarships": len(scholarships),
        "organizations": len(organizations),
        "total": len(saved_events) + len(personal_events) + len(scholarships) + len(organizations)
    }
    
    return result

@app.get("/users/me/saved-items")
async def get_all_saved_items_get(
    user_id: Optional[str] = None,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional)
):
    """
    Get all saved items from all collections under users/{uid}
    No authentication required - can pass user_id as query parameter
    """
    return await _get_saved_items_logic(user_id, current_user)

@app.post("/users/me/saved-items")
async def get_all_saved_items_post(
    payload: Optional[Dict[str, Any]] = None,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional)
):
    """
    Get all saved items from all collections under users/{uid}
    POST method - accepts user_id in request body
    No authentication required
    """
    user_id = payload.get("user_id") if payload else None
    return await _get_saved_items_logic(user_id, current_user)

# ----------------------------
# User Events Endpoints - New comprehensive events management
# Path: users/{user_id}/events
# ----------------------------

class EventModel(BaseModel):
    """Model for event data"""
    event_id: Optional[str] = None  # Auto-generated if not provided
    title: str
    description: Optional[str] = None
    start_date: str  # ISO format datetime
    end_date: Optional[str] = None
    location: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    created_by_me: bool = False  # True if user created this event, False if saved from external source
    is_saved: bool = False  # Whether this is a saved external event (deprecated, use created_by_me instead)
    source: Optional[str] = None  # e.g., "university", "user_created", "external"
    metadata: Optional[Dict[str, Any]] = None  # Additional event data

@app.post("/users/{user_id}/events")
async def create_user_event(
    user_id: str,
    event: EventModel,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Create or save an event for a user
    - For new events: Creates a new event document
    - For saved events: Saves reference to existing event
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        # Verify user has permission (can only add events to their own collection)
        current_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
        if current_uid != user_id:
            raise HTTPException(status_code=403, detail="Cannot add events to another user's collection")
        
        # Generate event ID if not provided
        event_id = event.event_id or f"event_{int(time.time() * 1000)}"
        
        # Prepare event data
        event_data = event.dict()
        event_data["event_id"] = event_id
        event_data["created_at"] = firestore.SERVER_TIMESTAMP
        event_data["updated_at"] = firestore.SERVER_TIMESTAMP
        
        # Save to users/{user_id}/events/{event_id}
        doc_ref = db.collection("users").document(user_id).collection("events").document(event_id)
        doc_ref.set(event_data)
        
        logger.info(f"Event created/saved for user {user_id}: {event_id}")
        
        return {
            "success": True,
            "message": "Event saved successfully",
            "event_id": event_id,
            "user_id": user_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving event for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error saving event: {str(e)}")

@app.get("/users/{user_id}/events")
async def get_user_events(
    user_id: str,
    category: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Get all events for a user with optional filtering
    - category: Filter by event category
    - start_date: Filter events starting after this date (ISO format)
    - end_date: Filter events ending before this date (ISO format)
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        # Verify user has permission
        current_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
        if current_uid != user_id:
            raise HTTPException(status_code=403, detail="Cannot access another user's events")
        
        # Query events collection
        events_ref = db.collection("users").document(user_id).collection("events")
        
        # Apply filters if provided
        query = events_ref
        if category:
            query = query.where("category", "==", category)
        
        # Get all events
        docs = query.stream()
        events = []
        
        for doc in docs:
            event_data = doc.to_dict()
            
            # Apply date filtering if needed
            if start_date or end_date:
                event_start = event_data.get("start_date")
                if event_start:
                    try:
                        event_start_dt = datetime.fromisoformat(event_start.replace('Z', '+00:00'))
                        
                        if start_date:
                            filter_start = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                            if event_start_dt < filter_start:
                                continue
                        
                        if end_date:
                            filter_end = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                            if event_start_dt > filter_end:
                                continue
                    except Exception as e:
                        logger.warning(f"Error parsing date for event {doc.id}: {e}")
            
            events.append(event_data)
        
        logger.info(f"Retrieved {len(events)} events for user {user_id}")
        
        return {
            "success": True,
            "events": events,
            "count": len(events),
            "user_id": user_id,
            "filters": {
                "category": category,
                "start_date": start_date,
                "end_date": end_date
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving events for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error retrieving events: {str(e)}")

@app.get("/users/{user_id}/events/{event_id}")
async def get_user_event(
    user_id: str,
    event_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Get a specific event for a user"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        # Verify user has permission
        current_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
        if current_uid != user_id:
            raise HTTPException(status_code=403, detail="Cannot access another user's events")
        
        # Get event document
        doc_ref = db.collection("users").document(user_id).collection("events").document(event_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Event not found")
        
        event_data = doc.to_dict()
        
        return {
            "success": True,
            "event": event_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving event {event_id} for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error retrieving event: {str(e)}")

@app.put("/users/{user_id}/events/{event_id}")
async def update_user_event(
    user_id: str,
    event_id: str,
    event: EventModel,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Update an existing event"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        # Verify user has permission
        current_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
        if current_uid != user_id:
            raise HTTPException(status_code=403, detail="Cannot update another user's events")
        
        # Check if event exists
        doc_ref = db.collection("users").document(user_id).collection("events").document(event_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Event not found")
        
        # Update event data
        event_data = event.dict(exclude_unset=True)
        event_data["updated_at"] = firestore.SERVER_TIMESTAMP
        
        doc_ref.update(event_data)
        
        logger.info(f"Event updated for user {user_id}: {event_id}")
        
        return {
            "success": True,
            "message": "Event updated successfully",
            "event_id": event_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating event {event_id} for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating event: {str(e)}")

@app.delete("/users/{user_id}/events/{event_id}")
async def delete_user_event(
    user_id: str,
    event_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Delete an event"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        # Verify user has permission
        current_uid = current_user.get("uid") or current_user.get("user_id") or current_user.get("sub")
        if current_uid != user_id:
            raise HTTPException(status_code=403, detail="Cannot delete another user's events")
        
        # Delete event document
        doc_ref = db.collection("users").document(user_id).collection("events").document(event_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Event not found")
        
        doc_ref.delete()
        
        logger.info(f"Event deleted for user {user_id}: {event_id}")
        
        return {
            "success": True,
            "message": "Event deleted successfully",
            "event_id": event_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting event {event_id} for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting event: {str(e)}")

# ----------------------------
# Remaining endpoints preserved (Chat, majors, profile etc. from original)
# Note: I preserved core functions; if you want the rest of original file's endpoints
# (websockets, streaming OpenAI, etc.), we can re-add those blocks unchanged.
# ----------------------------

# The merged file keeps original endpoints and logic, but uses university-aware loader where appropriate.

# ----------------------------
# Background preloading to warm cache on startup
# ----------------------------
def _preload_university_data_background():
    """
    DEPRECATED: Background preload no longer needed with Firestore.
    Firestore queries are fast enough that preloading is unnecessary.
    Kept for backward compatibility but does nothing.
    """
    logger.info("Background preload skipped - using Firestore direct queries instead")
    pass

@app.on_event("startup")
async def startup_event():
    """FastAPI startup event - initialize Firebase."""
    logger.info("Application starting up...")
    
    # Ensure Firebase is initialized first (critical for health checks)
    try:
        db = initialize_firebase()
        if db:
            logger.info("✅ Firebase initialized successfully on startup")
            logger.info("✅ Using Firestore direct queries - no file preloading needed")
        else:
            logger.error("❌ Firebase initialization failed on startup - app may be unhealthy")
    except Exception as e:
        logger.error(f"❌ Firebase initialization error on startup: {e}")
    
    logger.info("🚀 Application startup complete - ready to serve requests")

# ----------------------------
# End of file
# ----------------------------
