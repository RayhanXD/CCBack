from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect
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
import firebase_admin
from firebase_admin import credentials, auth, firestore
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

# Initialize FastAPI app
app = FastAPI(
    title="Campus Connect API",
    description="Backend API for Campus Connect application",
    version="1.0.0"
)

# Initialize global variables
db = None
calendar_df = None

# CORS middleware
allowed_origins = [
    "http://localhost:3000",  # Local development
    "http://localhost:8081",  # Expo development
    "https://*.railway.app",  # Railway deployments
    "https://*.vercel.app",   # Vercel deployments
    "https://*.netlify.app",  # Netlify deployments
]

# Add frontend URL from environment if provided
frontend_url = os.getenv("FRONTEND_URL")
if frontend_url:
    allowed_origins.append(frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()

class TimeoutException(Exception):
    pass

@contextmanager
def timeout(seconds):
    def signal_handler(signum, frame):
        raise TimeoutException(f"Timed out after {seconds} seconds")
    signal.signal(signal.SIGALRM, signal_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)

# Initialize Firebase Admin SDK
print("Setting up Firebase...")
db = None

def initialize_firebase():
    """Initialize Firebase with retry logic"""
    global db
    
    if db is not None:
        return db
    
    # Try to initialize with firebase-key.json first
    key_file = "/app/firebase-key.json"
    if os.path.exists(key_file):
        print(f" Found Firebase key file at {key_file}")
        try:
            cred = credentials.Certificate(key_file)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            print(" Firebase initialized successfully from JSON file")
            return db
        except Exception as e:
            print(f" Failed to initialize Firebase from JSON file: {str(e)}")
    else:
        print(f" Firebase key file not found at {key_file}")
    
    # Fallback to environment variables
    print(" Checking for Firebase environment variables...")
    project_id = os.getenv("FIREBASE_PROJECT_ID")
    private_key = os.getenv("FIREBASE_PRIVATE_KEY")
    client_email = os.getenv("FIREBASE_CLIENT_EMAIL")
    
    if project_id and private_key and client_email:
        print("Found Firebase environment variables")
        try:
            firebase_config = {
                "type": "service_account",
                "project_id": project_id,
                "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
                "private_key": private_key.replace('\\n', '\n'),
                "client_email": client_email,
                "client_id": os.getenv("FIREBASE_CLIENT_ID"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_X509_CERT_URL")
            }
            
            cred = credentials.Certificate(firebase_config)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            print(" Firebase initialized successfully from environment variables")
            return db
        except Exception as e:
            print(f" Failed to initialize Firebase from environment variables: {str(e)}")
            print("Please check your Firebase configuration and credentials.")
    else:
        print(" Missing required Firebase environment variables")
        print("Please set FIREBASE_PROJECT_ID, FIREBASE_PRIVATE_KEY, and FIREBASE_CLIENT_EMAIL environment variables.")
    
    # If we get here, all initialization attempts failed
    raise RuntimeError("Failed to initialize Firebase. Please check your configuration and try again.")

def get_db():
    """Helper function to get database connection with lazy initialization"""
    global db
    if db is not None:
        return db
        
    try:
        db = initialize_firebase()
        if db is None:
            raise RuntimeError("Failed to initialize Firebase: initialize_firebase() returned None")
        return db
    except Exception as e:
        error_msg = f"Failed to initialize Firebase: {str(e)}"
        print(error_msg)
        # Don't raise the exception here, let the calling function handle it
        return None

# ------------------------------
# Initialize OpenAI client
# ------------------------------

# Create a custom OpenAI client class that handles the proxies issue
class CustomOpenAI:
    def __init__(self, api_key):
        self.api_key = api_key
        self.chat = self.Chat(api_key)
    
    class Chat:
        def __init__(self, api_key):
            self.api_key = api_key
            self.completions = self.Completions(api_key)
        
        class Completions:
            def __init__(self, api_key):
                self.api_key = api_key
            
            def create(self, **kwargs):
                # Import here to avoid circular imports
                import httpx
                from openai import OpenAI
                
                # Create a custom HTTP client without proxies
                http_client = httpx.Client()
                
                # Create a real OpenAI client with the custom HTTP client
                try:
                    real_client = OpenAI(
                        api_key=self.api_key,
                        http_client=http_client
                    )
                    
                    # Forward the call to the real client
                    return real_client.chat.completions.create(**kwargs)
                except Exception as e:
                    print(f"Error in OpenAI API call: {e}")
                    # Create a mock response for error cases
                    class MockResponse:
                        class Choice:
                            class Message:
                                content = f"Error: {str(e)}"
                            message = Message()
                        choices = [Choice()]
                    return MockResponse()

try:
    # Get API key from environment
    openai_api_key = os.getenv("OPENAI_API_KEY")
    prompt_template_id = os.getenv("PROMPT_TEMPLATE_ID")
    
    # If environment variable doesn't work, try reading directly from .env file
    if not openai_api_key or openai_api_key == "your-api-key-here":
        try:
            with open('.env', 'r') as f:
                env_content = f.read()
                for line in env_content.splitlines():
                    if line.startswith('OPENAI_API_KEY='):
                        openai_api_key = line.split('=', 1)[1].strip()
                        print("Using API key from .env file directly")
                    elif line.startswith('PROMPT_TEMPLATE_ID=') and not prompt_template_id:
                        prompt_template_id = line.split('=', 1)[1].strip()
                        print("Using prompt template ID from .env file directly")
        except Exception as env_error:
            print(f"Error reading .env file: {env_error}")
    
    # Initialize custom OpenAI client
    if openai_api_key:
        client = CustomOpenAI(api_key=openai_api_key)
        print("Custom OpenAI client initialized successfully")
        
        if prompt_template_id:
            print(f"Prompt template ID loaded: {prompt_template_id}")
        else:
            print("Warning: PROMPT_TEMPLATE_ID not found in environment variables")
    else:
        print("Warning: OPENAI_API_KEY not found in environment variables or .env file")
        client = None
        
except Exception as e:
    print(f"Error in OpenAI initialization: {str(e)}")
    client = None
    prompt_template_id = None

# Pydantic models
class UserProfile(BaseModel):
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

class TokenVerification(BaseModel):
    token: str

class RecommendationRequest(BaseModel):
    user_email: EmailStr
    category: str = "orgs"  # orgs, events, tutoring

class ScholarshipRequest(BaseModel):
    user_email: EmailStr
    scholarships_data: List[Dict[str, Any]]

# ------------------------------
# Message Models
# ------------------------------
class ChatGPTMessage:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content

# Define a Pydantic model for ChatGPT messages
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
# ------------------------------
# Simple Connection Manager
# ------------------------------
class ChatGPTConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_email: str):
        await websocket.accept()
        self.active_connections[user_email] = websocket
        print(f"🔌 Connected: {user_email}")

    def disconnect(self, user_email: str):
        if user_email in self.active_connections:
            del self.active_connections[user_email]
            print(f" Disconnected: {user_email}")

    async def send_personal_message(self, message: str, user_email: str):
        websocket = self.active_connections.get(user_email)
        if websocket:
            await websocket.send_text(message)

# Initialize connection manager
chatgpt_manager = ChatGPTConnectionManager()

# List of undergraduate majors
majors = []
major_colors = {}

# Only try to access Firestore if db is not None
if db is not None:
    try:
        majors_doc = db.collection("majors").document("all_majors").get()
        if majors_doc.exists:
            majors_data = majors_doc.to_dict()
            # All Majors
            majors = majors_data.get("majors", [])
            # Major Colors Map
            major_colors = majors_data.get("major_colors", {})
        else:
            print("Majors not found")
            print(f"# of majors: {len(majors)}")
            print(f"# of colors: {len(major_colors)}")
    except Exception as e:
        print(f"Error accessing majors collection: {e}")
else:
    print("Database not initialized, using empty majors list")
    print(f"# of majors: {len(majors)}")
    print(f"# of colors: {len(major_colors)}")


# Default university shorthand
user_university = "utd"  # Default to UTD (University of Texas at Dallas)

# categorize majors by school
cat_majors = {}

# Only try to access Firestore if db is not None
if db is not None:
    try:
        cat_majors_doc = db.collection("university").document(user_university).get()
        if cat_majors_doc.exists:
            cat_majors_data = cat_majors_doc.to_dict()
            cat_majors = cat_majors_data.get("categorize_by_school", {})
        else:
            print("University categorization not found")
    except Exception as e:
        print(f"Error accessing university collection: {e}")
else:
    print("Database not initialized, using empty categorization")
""" (commented out file system)
# Load data function
def load_data():
    global calendar_df
    
    try:
        # Get university data from Firestore
        if not db:
            print("Database not initialized, cannot load data")
            return None, None, None, None, None, None
        
        # Get the university document for the current university
        university_doc = db.collection("university").document('utd').get()
        
        if not university_doc.exists:
            print(f"University document for {user_university} not found")
            return None, None, None, None, None, None
        
        university_data = university_doc.to_dict()
        data_file_urls = university_data.get("data_files", {})

        print(data_file_urls)
        
        if not data_file_urls:
            print("No data file URLs found in university document")
            return None, None, None, None, None, None
        
        print(f"Found data file URLs in university collection")
        
        # Initialize dataframes
        activities_df = None
        orgs_df = None
        events_df = None
        courses_df = None
        calendar_df = None
        tutoring_df = None
        
        # Load activities_df from URL
        if "activities_df" in data_file_urls:
            try:
                url = data_file_urls["activities_df"]
                response = requests.get(url)
                if response.status_code == 200:
                    activities_df = pd.read_csv(io.StringIO(response.text))
                    if 'List of Interests' in activities_df.columns:
                        activities_df['List of Interests'] = activities_df['List of Interests'].apply(ast.literal_eval)
                    print("Activities data loaded successfully")
                else:
                    print(f"Failed to download activities data: {response.status_code}")
            except Exception as e:
                print(f"Error loading activities data: {e}")
        
        # Load orgs_df from URL
        if "orgs_df" in data_file_urls:
            try:
                url = data_file_urls["orgs_df"]
                response = requests.get(url)
                if response.status_code == 200:
                    orgs_df = pd.read_csv(io.StringIO(response.text))
                    print("Organizations data loaded successfully")
                else:
                    print(f"Failed to download organizations data: {response.status_code}")
            except Exception as e:
                print(f"Error loading organizations data: {e}")
        
        # Load events_df from URL
        if "events_df" in data_file_urls:
            try:
                url = data_file_urls["events_df"]
                response = requests.get(url)
                if response.status_code == 200:
                    events_df = pd.read_csv(io.StringIO(response.text))
                    print("Events data loaded successfully")
                else:
                    print(f"Failed to download events data: {response.status_code}")
            except Exception as e:
                print(f"Error loading events data: {e}")
        
        # Load courses_df from URL
        if "courses_df" in data_file_urls:
            try:
                url = data_file_urls["courses_df"]
                response = requests.get(url)
                if response.status_code == 200:
                    courses_df = pd.read_csv(io.StringIO(response.text))
                    print("Courses data loaded successfully")
                else:
                    print(f"Failed to download courses data: {response.status_code}")
            except Exception as e:
                print(f"Error loading courses data: {e}")
        
        # Load calendar_df from URL
        if "calendar_df" in data_file_urls:
            try:
                url = data_file_urls["calendar_df"]
                response = requests.get(url)
                if response.status_code == 200:
                    calendar_df = pd.read_csv(io.StringIO(response.text))
                    print("Calendar data loaded successfully")
                else:
                    print(f"Failed to download calendar data: {response.status_code}")
            except Exception as e:
                print(f"Error loading calendar data: {e}")
        
        # Fallback to local file if calendar_df is still None
        if calendar_df is None:
            try:
                if os.path.exists("utd_events.csv"):
                    calendar_df = pd.read_csv("utd_events.csv")
                    print("Calendar data loaded from local file")
                else:
                    print("Local calendar data file not found")
            except Exception as e:
                print(f"Error loading local calendar data: {e}")
        
        # Load tutoring_df from URL (special case for Excel file)
        if "tutoring_df" in data_file_urls:
            try:
                url = data_file_urls["tutoring_df"]
                response = requests.get(url)
                if response.status_code == 200:
                    # Save the Excel file to a temporary file
                    with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as temp_file:
                        temp_file.write(response.content)
                        temp_path = temp_file.name
                    
                    # Read the Excel file from the temporary file
                    tutoring_df = pd.read_excel(temp_path, engine="openpyxl")
                    
                    # Remove the temporary file
                    os.unlink(temp_path)
                    
                    print("Tutoring data loaded successfully")
                else:
                    print(f"Failed to download tutoring data: {response.status_code}")
            except Exception as e:
                print(f"Error loading tutoring data: {e}")
        
        print("All available data loaded successfully")
        return activities_df, tutoring_df, orgs_df, events_df, courses_df, calendar_df
    except Exception as e:
        print(f"Error loading data: {e}")
        return None, None, None, None, None, None

#Load University Data
def university_data():
    # Just call the load_data function to maintain consistency
    return load_data()
"""

def load_data():
    global calendar_df
    
    try:
        # Check if data files exist
        data_files = [
            "CC_activities_ex.csv",
            "organizations_with_specific_majors.csv", 
            "filtered_utd_events_with_categories.csv",
            "utd_courses.csv",
            "UTD_tutoring.xlsx",
            "utd_events.csv"
        ]
        
        missing_files = []
        for file in data_files:
            if not os.path.exists(file):
                missing_files.append(file)
        
        if missing_files:
            print(f"Warning: Missing data files: {missing_files}")
            print("Some features may not work properly")
        
        # Load available files
        activities_df = pd.read_csv("CC_activities_ex.csv") if os.path.exists("CC_activities_ex.csv") else None
        orgs_df = pd.read_csv("organizations_with_specific_majors.csv") if os.path.exists("organizations_with_specific_majors.csv") else None
        events_df = pd.read_csv("filtered_utd_events_with_categories.csv") if os.path.exists("filtered_utd_events_with_categories.csv") else None
        courses_df = pd.read_csv("utd_courses.csv") if os.path.exists("utd_courses.csv") else None
        tutoring_df = pd.read_excel("UTD_tutoring.xlsx", engine="openpyxl") if os.path.exists("UTD_tutoring.xlsx") else None
        calendar_df = pd.read_csv("utd_events.csv") if os.path.exists("utd_events.csv") else None
        
        if activities_df is not None and 'List of Interests' in activities_df.columns:
            activities_df['List of Interests'] = activities_df['List of Interests'].apply(ast.literal_eval)
        
        print("Data loaded successfully")
        return activities_df, tutoring_df, orgs_df, events_df, courses_df, calendar_df
    except Exception as e:
        print(f"Error loading data: {e}")
        return None, None, None, None, None, None

# Load data at startup
activities_df, tutoring_df, orgs_df, events_df, courses_df, calendar_df = load_data()

# Process categories
if orgs_df is not None:
    categories = orgs_df['Category'].dropna().unique()
    cleaned_categories = []
    for category in categories:
        category = re.sub(r'[\[\]"]', '', category).strip()
        cleaned_categories.extend([item.strip() for item in re.split(r'[;,\.]', category)])
    options = sorted(set(cleaned_categories))
else:
    options = []

# Define default UTD majors by college
utd_majors = {
    "School of Arts, Humanities, and Technology": ["Arts", "Humanities", "Technology"],
    "Naveen Jindal School of Management": ["Business", "Management", "Finance", "Accounting"],
    "Erik Jonsson School of Engineering and Computer Science": ["Computer Science", "Software Engineering", "Computer Engineering", "Electrical Engineering"],
    "School of Natural Sciences and Mathematics": ["Mathematics", "Physics", "Chemistry", "Biology"],
    "School of Behavioral and Brain Sciences": ["Psychology", "Neuroscience", "Cognitive Science"],
    "School of Economic, Political and Policy Sciences": ["Economics", "Political Science", "Public Policy"]
}

# Utility functions
def get_college_by_major(major):
    for college, majors_list in utd_majors.items():
        if major in majors_list:
            return college
    return "Any College"

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

def get_personalized_scholarships(user_data, scholarships_data):
    """
    Generate personalized scholarship recommendations using GPT-4
    """
    if not client:
        print("OpenAI client not available")
        return []
    
    # Construct a prompt for GPT
    prompt = f"""
    Given a student with the following profile:
    - Major: {user_data.get('major', 'Undeclared')}
    - Year: {user_data.get('year', '1')}
    - GPA: {user_data.get('current_gpa', '0.0')}
    - Race/Ethnicity: {user_data.get('race_ethnicity', 'Unknown')}
    - Gender: {user_data.get('gender', 'Unknown')}
    - First Generation Status: {user_data.get('ftcs_status', 'No')}
    - Financial Factors: {user_data.get('financial_factors', 'N/A')}
    
    Review the following scholarships and return only the most relevant ones for this student. 
    For each scholarship, provide a brief explanation of why it's a good match.
    
    Scholarships to evaluate:
    {json.dumps(scholarships_data, indent=2)}
    
    Return the response in the following JSON format:
    {{
        "recommended_scholarships": [
            {{
                "name": "Scholarship Name",
                "amount": "Amount",
                "deadline": "YYYY-MM-DD",
                "match_score": 85,
                "explanation": "Brief reason for match",
                "original_data": {{}}
            }}
        ]
    }}
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a scholarship matching expert who helps students find the most relevant scholarships based on their profile."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        
        # Parse the response
        recommendations = json.loads(response.choices[0].message.content)
        return recommendations["recommended_scholarships"]
    except Exception as e:
        print(f"Error in getting scholarship recommendations: {e}")
        return []



async def generate_chatgpt_response(messages, model="gpt-4o-mini", temperature=0.7, max_tokens=150, stream=False):
    """
    Generate a response from ChatGPT using the OpenAI API
    """
    if not client:
        # If OpenAI client is not initialized, raise an exception
        # This will be caught by the calling function and returned as a 500 error
        raise Exception("OpenAI client is not initialized. Please check your API key configuration.")
        # We're not using a fallback message or mock client anymore as requested
    
    try:
        # Convert messages to the format expected by OpenAI API
        formatted_messages = [
            {"role": msg.role, "content": msg.content} for msg in messages
        ]
        
        # Add a system message if not present
        if not any(msg.role == "system" for msg in messages):
            formatted_messages.insert(0, {
                "role": "system",
                "content": "You are a helpful assistant for Campus Connect, a platform that helps college students find resources and connect with their campus community."
            })
        
        # Create the completion request with prompt template if available
        kwargs = {
            "model": model,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream
        }
        
        # Add prompt template ID if available
        if 'prompt_template_id' in globals() and prompt_template_id and not stream:
            kwargs["prompt_template_id"] = prompt_template_id
        
        response = client.chat.completions.create(**kwargs)
        
        if stream:
            print(f"Returning stream response of type: {type(response)}")
            async def stream_generator():
                print("Starting stream_generator")
                try:
                    for chunk in response:
                        print(f"Got chunk from stream: {chunk}")
                        yield chunk
                    print("stream_generator completed")
                except Exception as stream_error:
                    print(f"Error in stream_generator: {stream_error}")
                    # Yield an error chunk
                    yield {"choices": [{"delta": {"content": f"Error: {str(stream_error)}"}}]}
            
            print("Created stream_generator for OpenAI Stream object")
            return stream_generator()
        else:
            # Return the text response
            return response.choices[0].message.content
    except Exception as e:
        print(f"Error generating ChatGPT response: {e}")
        # Return a fallback message instead of raising an exception
        if stream:
            # For streaming, we need to create a mock stream
            class MockStream:
                async def __aiter__(self):
                    class MockChoice:
                        class MockDelta:
                            content = f"Error: {str(e)}. Please try again later or contact support."
                        delta = MockDelta()
                    yield type('MockChunk', (), {'choices': [MockChoice()]})()
            return MockStream()
        else:
            return f"Error: {str(e)}. Please try again later or contact support."


def _extract_content_from_chunk(chunk):
    """
    Extract content from a chunk based on its type.
    Returns None if no content could be extracted or if it's a final chunk.
    """
    content = None
    
    # Check if this is a final chunk with finish_reason='stop'
    is_final_chunk = False
    
    # Using object attributes (for OpenAI SDK objects)
    if hasattr(chunk, 'choices') and chunk.choices and len(chunk.choices) > 0:
        if hasattr(chunk.choices[0], 'finish_reason') and chunk.choices[0].finish_reason == 'stop':
            is_final_chunk = True
        elif hasattr(chunk.choices[0], 'delta') and hasattr(chunk.choices[0].delta, 'content'):
            content = chunk.choices[0].delta.content
    
    # Dictionary access (for dict-like objects)
    if content is None and isinstance(chunk, dict) and 'choices' in chunk:
        if chunk['choices'][0].get('finish_reason') == 'stop':
            is_final_chunk = True
        else:
            content = chunk['choices'][0].get('delta', {}).get('content', '')
    
    # Skip final chunks
    if is_final_chunk:
        return None
    
    #  Direct string conversion (fallback)
    if content is None and hasattr(chunk, '__str__'):
        try:
            chunk_str = str(chunk)
            if chunk_str and not chunk_str.startswith('<') and not chunk_str.endswith('>'):
                # Skip ChatCompletionChunk objects
                if 'ChatCompletionChunk' in chunk_str:
                    return None
                content = chunk_str
        except Exception:
            pass
    
    return content


async def _store_conversation(db_instance, user_email, messages, response, conversation_id, model):
    """Helper function to store conversation in Firestore"""
    if not db_instance or not response:
        return
        
    try:
        # Extract the user message
        user_message = next((msg.content for msg in messages if msg.role == "user"), "")
        
        # Create a new document in the user's conversation subcollection
        user_conversations_ref = db_instance.collection("chatgpt_conversations").document(user_email).collection("conversations")
        
        # Add the new conversation
        user_conversations_ref.add({
            "user_message": user_message,
            "assistant_response": response,
            "timestamp": datetime.now(),
            "conversation_id": conversation_id,
            "model": model
        })
        
        # Get the current count of conversations for this user
        conversations = user_conversations_ref.order_by("timestamp", direction=firestore.Query.ASCENDING).limit(11).stream()
        
        # Convert to list to count and access items
        conversation_list = list(conversations)
        
        # If there are more than 10 conversations, delete the oldest one
        if len(conversation_list) > 10:
            oldest_conversation = conversation_list[0]
            user_conversations_ref.document(oldest_conversation.id).delete()
            print(f"Deleted oldest conversation for user {user_email} to maintain 10 conversation limit")
            
    except Exception as e:
        print(f"Error storing conversation in Firestore: {e}")


# Dependency to get current user
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        decoded_token = auth.verify_id_token(credentials.credentials)
        return decoded_token
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials"
        )

# API Routes
@app.get("/")
async def root():
    return {"message": "Campus Connect API", "version": "1.0.0"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/majors")
async def get_majors():
    try:
        print("=== MAJORS ENDPOINT CALLED ===")
        database = get_db()
        print(f"Database object: {database}")
        
        if not database:
            print("❌ Database not available")
            return {"majors": [], "error": "Database not available"}
        
        print("✅ Database available, querying majors...")
        majors_doc = database.collection("majors").document("all_majors").get()
        print(f"Document exists: {majors_doc.exists}")
        
        if majors_doc.exists:
            majors_data = majors_doc.to_dict()
            majors_list = majors_data.get("majors", [])
            print(f"✅ Found {len(majors_list)} majors")
            return {"majors": majors_list}
        else:
            print("❌ Majors document not found")
            return {"majors": [], "error": "Document not found"}
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"majors": [], "error": str(e)}

@app.get("/debug-firebase")
async def debug_firebase():
    """Debug Firebase connection and document access"""
    try:
        database = get_db()
        if not database:
            return {"status": "error", "message": "Database not available"}
        
        # Test collections access
        collections = list(database.collections())
        collection_names = [col.id for col in collections]
        
        # Test majors document specifically
        majors_doc = database.collection("majors").document("all_majors").get()
        
        return {
            "status": "success",
            "firebase_connected": True,
            "collections": collection_names,
            "majors_doc_exists": majors_doc.exists,
            "majors_count": len(majors_doc.to_dict().get("majors", [])) if majors_doc.exists else 0
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/categories")
async def get_categories():
    return {"categories": options}

@app.get("/major-colors")
async def get_major_colors():
    return {"major_colors": major_colors}

@app.get("/organizations")
async def get_organizations():
    """
    Endpoint to get all organizations from the organizations dataframe.
    Returns all organizations without filtering or scoring.
    """
    if orgs_df is None:
        raise HTTPException(status_code=500, detail="Organizations data not available")
    
    try:
        # Create a copy to avoid modifying the original dataframe
        df_clean = orgs_df.copy()
        
        # Replace NaN, inf, and -inf values with None for JSON serialization
        df_clean = df_clean.replace([float('inf'), float('-inf')], None)
        df_clean = df_clean.where(pd.notna(df_clean), None)
        
        # Convert dataframe to list of dictionaries
        organizations = df_clean.to_dict(orient='records')
        
        # Additional cleanup: recursively replace any remaining problematic float values
        def clean_dict(d):
            if isinstance(d, dict):
                return {k: clean_dict(v) for k, v in d.items()}
            elif isinstance(d, list):
                return [clean_dict(item) for item in d]
            elif isinstance(d, float):
                if math.isnan(d) or math.isinf(d):
                    return None
                return d
            return d
        
        organizations = clean_dict(organizations)
        
        return {
            "organizations": organizations,
            "count": len(organizations)
        }
    except Exception as e:
        import traceback
        print(f"Error in get_organizations: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error retrieving organizations: {str(e)}")

@app.post("/signup")
async def signup(user_data: UserProfile):
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        user_email = user_data.email
        user_dict = user_data.dict()
        
        db.collection('users').document(user_email).set(user_dict)
        return {"message": "User created successfully", "email": user_email}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating user: {str(e)}")

@app.post("/signin")
async def signin(user_data: UserSignIn):
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        user_email = user_data.email
        user_doc = db.collection('users').document(user_email).get()
        # Load University Data of User


        if user_doc.exists:
            return {"message": "Sign-in successful", "user": user_doc.to_dict()}
        else:
            raise HTTPException(status_code=404, detail="User not found. Please sign up first.")
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

@app.get("/profile/{user_email}")
async def get_profile(user_email: str):
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        user_doc = db.collection('users').document(user_email).get()
        if user_doc.exists:
            return {"user": user_doc.to_dict()}
        else:
            raise HTTPException(status_code=404, detail="User not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving profile: {str(e)}")

@app.put("/profile/{user_email}")
async def update_profile(user_email: str, user_data: UserProfile):
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        user_dict = user_data.dict()
        db.collection('users').document(user_email).update(user_dict)
        return {"message": "Profile updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating profile: {str(e)}")

@app.post("/recommendations")
async def get_recommendations(request: RecommendationRequest):
    if not all([orgs_df is not None, events_df is not None, tutoring_df is not None]):
        raise HTTPException(status_code=500, detail="Data not available")
    
    database = get_db()
    if not database:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        # Get user data
        user_doc = database.collection('users').document(request.user_email).get()
        if not user_doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_data = user_doc.to_dict()
        
        # Extract user variables
        name = user_data.get('name', 'Unknown')
        surname = user_data.get('surname', 'Unknown')
        school_name = user_data.get('school_name', 'Unknown')
        year = user_data.get('year', '1')
        ftcs_status = user_data.get('ftcs_status', 'No')
        gpa_range = user_data.get('gpa_range', '<2.0')
        major = user_data.get('major', 'Undeclared')
        interests = user_data.get('interests', []) or []
        academic_difficulty = user_data.get('academic_difficulty', 'Moderate')
        stress_level = user_data.get('stress_level', 'Low')
        satisfaction = user_data.get('satisfaction', 'Neutral')
        self_efficacy = user_data.get('self_efficacy', 'Moderate')
        financial_factors = user_data.get('financial_factors', 'N/A')
        family_responsibilities = user_data.get('family_responsibilities', 'N/A')
        outside_encouragement = user_data.get('outside_encouragement', []) or []
        
        # Calculate support ratings
        def calculate_social_support():
            score = 0
            if academic_difficulty == "Difficult" or stress_level == "High":
                score += 2
            if "Peers" in outside_encouragement or "Community" in outside_encouragement:
                score -= 1
            if satisfaction == "Dissatisfied":
                score += 2
            return min(max(score, 1), 5)

        def calculate_intellectual_support():
            score = 0
            if gpa_range in ["< 2.0", "2.0 - 2.5"]:
                score += 3
            if academic_difficulty == "Difficult":
                score += 2
            if self_efficacy == "Little Belief":
                score += 2
            if "Teachers" in outside_encouragement:
                score -= 1
            return min(max(score, 1), 5)

        def calculate_career_development():
            score = 0
            if financial_factors in ["Work Income", "Loan"]:
                score += 1
            if satisfaction == "Neutral" or self_efficacy == "Some Belief":
                score += 1
            if family_responsibilities == "High":
                score += 1
            if "Family" in outside_encouragement:
                score -= 1
            return min(max(score, 1), 5)

        social_support_rating = calculate_social_support()
        intellectual_support_rating = calculate_intellectual_support()
        career_development_rating = calculate_career_development()

        # Scoring functions
        def score_orgs(df, school_name, year, ftcs_status, gpa_range, major, interests):
            user_college = get_college_by_major(major)
            scores = []
            explanations = []
            
            for _, row in df.iterrows():
                score = 0
                explanation_parts = []

                specific_majors = ast.literal_eval(row["Specific Majors"]) if row["Specific Majors"] else []

                # Handle None values in Category field
                category = row["Category"] or ""
                
                if year == "1" and category in ["Cultural", "Social", "Recreation"]:
                    score += social_support_rating/3
                    explanation_parts.append("This activity is ideal for first-year students to connect socially.")
                elif year in ["3", "4", "5+"] and category in ["Academic Interests", "Educational/Departmental"]:
                    score += 1
                    explanation_parts.append("This activity provides valuable educational and departmental experience for upper-year students.")

                # Check if interests match
                matched_interests = any(interest in category for interest in interests) if interests else False
                if matched_interests:
                    score += 1
                    explanation_parts.append("This activity aligns with your interests.")
                
                if row["Majors"] == user_college:
                    score += 2
                    explanation_parts.append("This activity is relevant to your college.")
                
                if row["Majors"] == 'any major':
                    score += .5
                    explanation_parts.append("This activity is open to all majors.")
                
                if major in specific_majors:
                    score += 3
                    explanation_parts.append("This activity directly aligns with your major.")

                score = round(score, 2)
                scores.append(score)
                explanations.append(" ".join(explanation_parts))

            df["Score"] = scores
            df["Recommendation Explanation"] = explanations
            top_results = df.sort_values(by="Score", ascending=False).head(7)
            return top_results

        def score_events(df, school_name, year, ftcs_status, gpa_range, major, interests):
            school_category_mappings = {
                "School of Arts, Humanities, and Technology": "Social",
                "Naveen Jindal School of Management": "Business",
                "Erik Jonsson School of Engineering and Computer Science": "STEM",
                "School of Natural Sciences and Mathematics": "STEM",
                "School of Behavioral and Brain Sciences": "STEM",
                "School of Economic, Political and Policy Sciences": "Business"
            }

            school = get_college_by_major(major)
            scores = []
            explanations = []

            for _, row in df.iterrows():
                score = 0
                explanation_parts = []

                if year == "1":
                    score += social_support_rating/3
                    explanation_parts.append("Social events can help first-year students build a network and feel more connected to the campus community.")

                if gpa_range == '<2.0' or gpa_range == '2.0 - 2.5':
                    score += intellectual_support_rating/3
                    explanation_parts.append("With a GPA below 2.5, tutoring is highly recommended to support your academic growth.")

                if year in ["3", "4", "5"]:
                    score += career_development_rating/3
                    explanation_parts.append("As a 3rd, 4th, or 5th-year student, career development opportunities can help you prepare for post-graduation goals.")

                if school in school_category_mappings:
                    category = school_category_mappings[school]
                    score += 1
                    explanation_parts.append(f"Being in the {school} makes this opportunity more relevant for {category}.")

                if ftcs_status:
                    score += 1
                    explanation_parts.append("As an FTC student, social events can help you integrate and feel more connected to the community.")

                score = round(score, 2)
                scores.append(score)
                explanations.append(" ".join(explanation_parts))

            df["Score"] = scores
            df["Recommendation Explanation"] = explanations
            top_results = df.sort_values(by="Score", ascending=False).head(7)
            return top_results

        def score_tutoring(df, school_name, year, ftcs_status, gpa_range, major, interests):
            scores = []
            explanations = []
            
            for _, row in df.iterrows():
                score = 0
                explanation_parts = []

                try:
                    majors = row['Majors']
                    majors = majors.split(", ")
                except (ValueError, SyntaxError):
                    majors = "All Majors"
                
                if major not in majors and majors != ['All Majors']:
                    scores.append(0)
                    explanations.append('This opportunity may not be for your major')
                    continue

                if major in majors: 
                    score += 1
                    explanation_parts.append("This opportunity is perfect for your major!")

                if year == "1":
                    score += 2
                    explanation_parts.append("Tutoring can be a fantastic resource for first-year students adapting to the college workload!")
                elif year == "2":
                    score += 1
                    explanation_parts.append("Tutoring is highly beneficial for underclassmen building a strong academic foundation.")

                if gpa_range == '<2.0' or gpa_range == '2.0 - 2.5':
                    score += intellectual_support_rating/3
                    explanation_parts.append("Tutoring can be a valuable tool to help you strengthen your academic performance and reach your goals!")
                elif gpa_range == '2.6 - 3.0':
                    score += intellectual_support_rating/3
                    explanation_parts.append("With a bit of extra support, you can build on your achievements and keep moving towards your academic potential.")
                elif gpa_range == '3.1 - 3.5':
                    score += intellectual_support_rating/3
                    explanation_parts.append("Tutoring can be a great way to maintain and even boost your already solid academic standing.")

                score = round(score, 2)
                scores.append(score)
                explanations.append(" ".join(explanation_parts))

            df["Score"] = scores
            df["Recommendation Explanation"] = explanations
            return df.sort_values(by="Score", ascending=False)

        # Get recommendations based on category
        if request.category == 'orgs':
            scored_df = score_orgs(orgs_df, school_name, year, ftcs_status, gpa_range, major, interests)
            recommendations = scored_df.to_dict(orient='records')
        elif request.category == 'events':
            scored_events = score_events(events_df, school_name, year, ftcs_status, gpa_range, major, interests)
            recommendations = scored_events.to_dict(orient='records')
            for item in recommendations:
                item['Formatted Start Time'] = format_datetime(item.get('Start Time', ''))
                item['Formatted End Time'] = format_datetime(item.get('End Time', ''))
                item['Event Name'] = extract_event_name(item.get('URL', ''))
        elif request.category == 'tutoring':
            scored_tutoring = score_tutoring(tutoring_df, school_name, year, ftcs_status, gpa_range, major, interests)
            filtered_tutoring = scored_tutoring[scored_tutoring["Score"] > 0]
            recommendations = filtered_tutoring.to_dict(orient='records')
        else:
            recommendations = []

        return {
            "recommendations": recommendations,
            "category": request.category,
            "major_colors": major_colors
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating recommendations: {str(e)}")

@app.post("/personalized-scholarships")
async def get_personalized_scholarships_endpoint(request: ScholarshipRequest):
    """
    Generate personalized scholarship recommendations using GPT-4
    """
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    if not client:
        raise HTTPException(status_code=500, detail="OpenAI service not available")
    
    try:
        # Get user data
        user_doc = db.collection('users').document(request.user_email).get()
        if not user_doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_data = user_doc.to_dict()
        
        # Get personalized scholarship recommendations
        recommendations = get_personalized_scholarships(user_data, request.scholarships_data)
        
        return {
            "recommendations": recommendations,
            "user_email": request.user_email,
            "total_recommendations": len(recommendations)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating personalized scholarships: {str(e)}")

#University API
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
        print(f"Error creating university: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create university: {str(e)}")

# Get a university by name
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
        print(f"Error fetching university: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch university: {str(e)}")


# Update a university
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
            
        # Convert the UniversityModel to a dictionary and remove None values
        update_data = {k: v for k, v in univ.dict().items() if v is not None}
        doc_ref.update(update_data)
        return {"message": "University updated successfully"}
    except Exception as e:
        print(f"Error updating university: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to update university: {str(e)}")


# Delete a university
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
        return {"message": "University deleted successfully"}
    except Exception as e:
        print(f"Error deleting university: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete university: {str(e)}")


# List all universities
@app.get("/universities")
def list_universities():
    try:
        print(" Attempting to list universities...")
        
        # Use get_db() to ensure the database is properly initialized
        print("Initializing database connection...")
        db_instance = get_db()
        if db_instance is None:
            error_msg = " Failed to initialize database connection: get_db() returned None"
            print(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)
        
        print("Accessing 'university' collection...")
        try:
            universities_ref = db_instance.collection("university")
            print("Successfully accessed 'university' collection")
            
            print("Fetching universities...")
            universities = universities_ref.stream()
            
            # Convert Firestore documents to dictionaries
            result = []
            for university in universities:
                try:
                    result.append(university.to_dict())
                except Exception as e:
                    print(f"Error converting university document: {str(e)}")
            
            print(f"Successfully retrieved {len(result)} universities")
            return result
            
        except Exception as e:
            error_msg = f" Error accessing Firestore collection: {str(e)}"
            print(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)
            
    except HTTPException:
        # Re-raise HTTP exceptions as they are
        raise
        
    except Exception as e:
        error_msg = f" Unexpected error in list_universities: {str(e)}"
        print(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)
  

# ------------------------------
# Streaming Function
# ------------------------------
async def stream_chatgpt_response(websocket: WebSocket, messages: List[ChatGPTMessage],
                                  user_email: str, model: str, temperature: float, max_tokens: int):
    """
    Streams the ChatGPT response token-by-token through WebSocket
    """
    try:
        # Prepare messages
        formatted_messages = [{"role": msg.role, "content": msg.content} for msg in messages]

        # Check if the client is properly initialized
        if not client:
            await websocket.send_text("Error: OpenAI client is not initialized")
            return
        
        # Get a non-streaming response and simulate streaming
        try:
            # Create a completion
            response = client.chat.completions.create(
                model=model,
                messages=formatted_messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            # Get the response content
            content = response.choices[0].message.content
            
            # Send the content in chunks to simulate streaming
            chunk_size = 4  # Send 4 characters at a time
            for i in range(0, len(content), chunk_size):
                chunk = content[i:i+chunk_size]
                await websocket.send_text(chunk)
                # Small delay to simulate streaming
                await asyncio.sleep(0.05)
            
            # Send end marker
            await websocket.send_text("\n[END]")
            
        except Exception as inner_e:
            print(f"Error in chat completion: {str(inner_e)}")
            await websocket.send_text(f"Error in chat completion: {str(inner_e)}")

    except Exception as e:
        error_message = f"Error in stream_chatgpt_response: {str(e)}"
        print(error_message)
        await websocket.send_text(error_message)

# ChatGPT API Endpoints

# ------------------------------
# WebSocket Endpoint
# ------------------------------
@app.websocket("/ws/chatgpt/{user_email}")
async def chatgpt_websocket(websocket: WebSocket, user_email: str):
    """
    WebSocket endpoint for streaming ChatGPT responses
    """
    try:
        # Connect WebSocket client
        await chatgpt_manager.connect(websocket, user_email)

        # Process incoming messages
        while True:
            data = await websocket.receive_text()

            try:
                message_data = json.loads(data)
                messages = []

                # Add system prompt if provided
                if "system" in message_data and message_data["system"]:
                    messages.append(ChatGPTMessage(role="system", content=message_data["system"]))

                # Add user message
                if "message" in message_data and message_data["message"]:
                    messages.append(ChatGPTMessage(role="user", content=message_data["message"]))
                else:
                    await websocket.send_text("Error: No message provided")
                    continue

                # Extract model parameters
                model = message_data.get("model", "gpt-4o-mini")
                temperature = message_data.get("temperature", 0.7)
                max_tokens = message_data.get("max_tokens", 150)

                # Stream response
                await stream_chatgpt_response(
                    websocket=websocket,
                    messages=messages,
                    user_email=user_email,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens
                )

            except json.JSONDecodeError:
                await websocket.send_text("Error: Invalid JSON format")
            except Exception as e:
                await websocket.send_text(f"Error: {str(e)}")

    except WebSocketDisconnect:
        chatgpt_manager.disconnect(user_email)
        print(f"Client disconnected: {user_email}")

@app.post("/chatgpt/chat")
async def chatgpt_chat(request: ChatGPTRequest):
    """
    POST endpoint for ChatGPT responses
    """
    try:
        # Check if OpenAI client is available
        if not client:
            raise HTTPException(status_code=500, detail="OpenAI service not available")
        
        # Generate response
        response_text = await generate_chatgpt_response(
            messages=request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=False
        )
        
        # Generate a conversation ID
        conversation_id = str(datetime.now().timestamp())
        
        # Store the conversation in Firestore if database is available
        if db:
            try:
                # Get the user message
                user_message = next((msg.content for msg in request.messages if msg.role == "user"), "")
                
                # Create a reference to the user's conversation subcollection
                user_conversations_ref = db.collection("chatgpt_conversations").document(request.user_email).collection("conversations")
                
                # Add the new conversation
                user_conversations_ref.add({
                    "user_message": user_message,
                    "assistant_response": response_text,
                    "timestamp": datetime.now(),
                    "conversation_id": conversation_id,
                    "model": request.model
                })
                
                # Get the current count of conversations for this user
                conversations = user_conversations_ref.order_by("timestamp", direction=firestore.Query.ASCENDING).limit(11).stream()
                
                # Convert to list to count and access items
                conversation_list = list(conversations)
                
                # If there are more than 10 conversations, delete the oldest one
                if len(conversation_list) > 10:
                    oldest_conversation = conversation_list[0]
                    user_conversations_ref.document(oldest_conversation.id).delete()
                    print(f"Deleted oldest conversation for user {request.user_email} to maintain 10 conversation limit")
            except Exception as e:
                print(f"Error storing conversation in Firestore: {e}")
        
        # Return the response
        return {
            "user_email": request.user_email,
            "message": response_text,
            "timestamp": datetime.now().isoformat(),
            "conversation_id": conversation_id
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating response: {str(e)}")

@app.get("/chatgpt/history/{user_email}")
async def get_chatgpt_history(user_email: str, limit: int = 10):
    """
    Get chat history for a specific user
    """
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        # Get reference to the user's conversation subcollection
        user_conversations_ref = db.collection("chatgpt_conversations").document(user_email).collection("conversations")
        
        # Query the subcollection for chat history
        conversations_ref = user_conversations_ref.order_by(
            "timestamp", direction=firestore.Query.DESCENDING
        ).limit(limit)
        
        # Get the conversations
        conversations = conversations_ref.stream()
        
        # Convert to list of dictionaries
        history = []
        for conv in conversations:
            conv_data = conv.to_dict()
            conv_data["id"] = conv.id
            if "timestamp" in conv_data and isinstance(conv_data["timestamp"], datetime):
                conv_data["timestamp"] = conv_data["timestamp"].isoformat()
            history.append(conv_data)
        
        return {"history": history, "count": len(history)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving chat history: {str(e)}")

@app.delete("/chatgpt/history/{user_email}/{conversation_id}")
async def delete_chatgpt_conversation(user_email: str, conversation_id: str, current_user: dict = Depends(get_current_user)):
    """
    Delete a specific ChatGPT conversation for a user
    """
    # Validate that the current user is deleting their own conversation
    if current_user.get("email") != user_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own conversations"
        )
    
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        # Get reference to the user's conversation subcollection
        user_conversations_ref = db.collection("chatgpt_conversations").document(user_email).collection("conversations")
        
        # Find the conversation with the matching conversation_id
        query = user_conversations_ref.where("conversation_id", "==", conversation_id).limit(1)
        conversations = query.stream()
        
        # Check if any matching conversation was found
        conversation_docs = list(conversations)
        if not conversation_docs:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # Delete the conversation document
        conversation_doc = conversation_docs[0]
        user_conversations_ref.document(conversation_doc.id).delete()
        
        return {"message": "Conversation deleted successfully", "conversation_id": conversation_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting conversation: {str(e)}")

@app.delete("/chatgpt/history/{user_email}")
async def delete_all_chatgpt_conversations(user_email: str, current_user: dict = Depends(get_current_user)):
    """
    Delete all ChatGPT conversations for a user
    """
    # Validate that the current user is deleting their own conversations
    if current_user.get("email") != user_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own conversations"
        )
    
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        # Get reference to the user's conversation subcollection
        user_conversations_ref = db.collection("chatgpt_conversations").document(user_email).collection("conversations")
        
        # Get all conversations for the user
        conversations = user_conversations_ref.stream()
        
        # Delete each conversation document
        deleted_count = 0
        for conversation in conversations:
            user_conversations_ref.document(conversation.id).delete()
            deleted_count += 1
        
        return {"message": "All conversations deleted successfully", "deleted_count": deleted_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting conversations: {str(e)}")

@app.post("/calendar")
async def get_calendar_events(request: CalendarRequest = None):
    """
    Endpoint to get calendar events from utd_events.csv.
    
    Returns events in the format required by the React app:
    {
      id: string;
      title: string;
      date: string; // ISO date string
      time: string; // Formatted time string (e.g., "1:00 PM")
      duration: number; // in minutes
      location: string;
      description?: string;
      color?: string;
    }
    """
    # Safely access the global calendar_df which may not be defined if startup failed early
    global calendar_df
    try:
        df = calendar_df  # type: ignore[name-defined]
    except NameError:
        df = None

    if df is None:
        # Try to load from local file as fallback
        try:
            if os.path.exists("utd_events.csv"):
                df = pd.read_csv("utd_events.csv")
                calendar_df = df  # Update global variable
                print("Calendar data loaded from local file for /calendar endpoint")
            else:
                raise HTTPException(status_code=500, detail="Calendar data not available")
        except Exception as e:
            print(f"Error loading local calendar data: {e}")
            raise HTTPException(status_code=500, detail="Calendar data not available")
    
    # Create a copy of the dataframe to avoid modifying the original
    filtered_events = df.copy()
    
    # Apply filters if provided
    if request:
        if request.start_date:
            try:
                start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
                filtered_events = filtered_events[
                    pd.to_datetime(filtered_events['start_date']).dt.date >= start_date.date()
                ]
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid start_date format. Use YYYY-MM-DD")
        
        if request.end_date:
            try:
                end_date = datetime.strptime(request.end_date, "%Y-%m-%d")
                filtered_events = filtered_events[
                    pd.to_datetime(filtered_events['start_date']).dt.date <= end_date.date()
                ]
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid end_date format. Use YYYY-MM-DD")
        
        if request.categories and len(request.categories) > 0:
            # Filter events that have at least one of the requested categories
            filtered_events = filtered_events[
                filtered_events['categories'].apply(
                    lambda x: any(cat in str(x).split(',') for cat in request.categories) if pd.notna(x) else False
                )
            ]
        
        if request.location:
            # Case-insensitive partial match for location
            filtered_events = filtered_events[
                filtered_events['location_name'].str.contains(request.location, case=False, na=False) |
                filtered_events['location_address'].str.contains(request.location, case=False, na=False)
            ]
    
    # Convert to the required format for the React app
    events_list = []
    for _, event in filtered_events.iterrows():
        # Generate a unique ID
        event_id = str(hash(event['name'] + str(event['start_date'])))[:8]
        
        # Parse start and end dates
        start_date = pd.to_datetime(event['start_date'])
        end_date = pd.to_datetime(event['end_date']) if pd.notna(event['end_date']) else start_date
        
        # Calculate duration in minutes
        duration = int((end_date - start_date).total_seconds() / 60)
        if duration <= 0:
            duration = 60  # Default to 1 hour if no duration or invalid
        
        # Format time as "1:00 PM"
        time_str = start_date.strftime("%I:%M %p").lstrip("0")
        
        # Combine location name and address
        location = ""
        if pd.notna(event['location_name']) and event['location_name']:
            location = event['location_name']
        if pd.notna(event['location_address']) and event['location_address']:
            if location:
                location += ", " + event['location_address']
            else:
                location = event['location_address']
        
        # Determine color based on category (optional)
        color = None
        if pd.notna(event['categories']):
            categories = str(event['categories']).split(',')
            if categories:
                # Simple mapping of categories to colors
                category_colors = {
                    "Arts & Performances": "#FF5733",
                    "Career Development": "#33FF57",
                    "Academic": "#3357FF",
                    "Social": "#FF33A8",
                    "Sports": "#33A8FF",
                    "Community Service": "#A833FF"
                }
                # Use the first category that has a defined color
                for cat in categories:
                    if cat.strip() in category_colors:
                        color = category_colors[cat.strip()]
                        break
        
        # Create event object
        calendar_event = {
            "id": event_id,
            "title": event['name'],
            "date": start_date.strftime("%Y-%m-%d"),
            "time": time_str,
            "duration": duration,
            "location": location,
            "description": event['description'] if pd.notna(event['description']) else None,
            "color": color,
            "img": event['image'] if pd.notna(event['image']) else None,
        }
        
        events_list.append(calendar_event)
    
    return {"events": events_list, "count": len(events_list)}
@app.get("/today-events")
async def get_today_events():
    global calendar_df
    """
    Endpoint to get today's events from utd_events.csv.
    
    Returns events for today's date in the same format as the calendar endpoint:
    {
      id: string;
      title: string;
      date: string; // ISO date string
      time: string; // Formatted time string (e.g., "1:00 PM")
      duration: number; // in minutes
      location: string;
      description?: string;
      color?: string;
    }
    """
    if calendar_df is None:
        # Try to load from local file as fallback
        try:
            if os.path.exists("utd_events.csv"):
                calendar_df = pd.read_csv("utd_events.csv")
                print("Calendar data loaded from local file for today-events")
            else:
                raise HTTPException(status_code=500, detail="Calendar data not available")
        except Exception as e:
            print(f"Error loading local calendar data: {e}")
            raise HTTPException(status_code=500, detail="Calendar data not available")
    
    # Create a copy of the dataframe to avoid modifying the original
    filtered_events = calendar_df.copy()
    
    # Get today's date
    today = datetime.now().date()
    
    # Filter events for today's date
    filtered_events = filtered_events[
        pd.to_datetime(filtered_events['start_date']).dt.date == today
    ]
    
    # Convert to the required format for the React app
    events_list = []
    for _, event in filtered_events.iterrows():
        # Generate a unique ID
        event_id = str(hash(event['name'] + str(event['start_date'])))[:8]
        
        # Parse start and end dates
        start_date = pd.to_datetime(event['start_date'])
        end_date = pd.to_datetime(event['end_date']) if pd.notna(event['end_date']) else start_date
        
        # Calculate duration in minutes
        duration = int((end_date - start_date).total_seconds() / 60)
        if duration <= 0:
            duration = 60  # Default to 1 hour if no duration or invalid
        
        # Format time as "1:00 PM"
        time_str = start_date.strftime("%I:%M %p").lstrip("0")
        
        # Combine location name and address
        location = ""
        if pd.notna(event['location_name']) and event['location_name']:
            location = event['location_name']
        if pd.notna(event['location_address']) and event['location_address']:
            if location:
                location += ", " + event['location_address']
            else:
                location = event['location_address']
        
        # Determine color based on category (optional)
        color = None
        if pd.notna(event['categories']):
            categories = str(event['categories']).split(',')
            if categories:
                # Simple mapping of categories to colors
                category_colors = {
                    "Arts & Performances": "#FF5733",
                    "Career Development": "#33FF57",
                    "Academic": "#3357FF",
                    "Social": "#FF33A8",
                    "Sports": "#33A8FF",
                    "Community Service": "#A833FF"
                }
                # Use the first category that has a defined color
                for cat in categories:
                    if cat.strip() in category_colors:
                        color = category_colors[cat.strip()]
                        break
        
        # Create event object
        calendar_event = {
            "id": event_id,
            "title": event['name'],
            "date": start_date.strftime("%Y-%m-%d"),
            "time": time_str,
            "duration": duration,
            "location": location,
            "description": event['description'] if pd.notna(event['description']) else None,
            "color": color,
            "img": event['image'] if pd.notna(event['image']) else None,
        }
        
        events_list.append(calendar_event)
    
    return {"events": events_list, "count": len(events_list)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
