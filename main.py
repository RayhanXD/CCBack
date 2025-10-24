from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
import pandas as pd
from urllib.parse import urlparse
import ast
import json
import re
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, auth, firestore
from pydantic import BaseModel, EmailStr
from typing import List, Optional, Dict, Any
import os
from openai import OpenAI

# Initialize FastAPI app
app = FastAPI(
    title="Campus Connect API",
    description="Backend API for Campus Connect application",
    version="1.0.0"
)

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

# Initialize Firebase Admin SDK
try:
    # Check if running in Railway (production)
    if os.getenv("RAILWAY_ENVIRONMENT"):
        # Use environment variables for Railway
        firebase_config = {
            "type": "service_account",
            "project_id": os.getenv("FIREBASE_PROJECT_ID"),
            "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
            "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace('\\n', '\n'),
            "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "client_id": os.getenv("FIREBASE_CLIENT_ID"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_X509_CERT_URL")
        }
        cred = credentials.Certificate(firebase_config)
    else:
        # Use local file for development
        cred = credentials.Certificate("firebase-key.json")
    
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Firebase initialized successfully")
except Exception as e:
    print(f"Firebase initialization error: {e}")
    db = None

# Initialize OpenAI client
try:
    # Try to get API key from environment variable
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
    
    if openai_api_key:
        try:
            # Simple initialization with just the API key
            client = OpenAI(api_key=openai_api_key)
            print("OpenAI client initialized successfully")
            if prompt_template_id:
                print(f"Prompt template ID loaded: {prompt_template_id}")
            else:
                print("Warning: PROMPT_TEMPLATE_ID not found in environment variables")
        except Exception as e:
            print(f"OpenAI client initialization error: {e}")
            # Fallback to basic initialization
            try:
                # Import directly to ensure we're using the right version
                from openai import OpenAI as OpenAIClient
                client = OpenAIClient(api_key=openai_api_key)
                print("OpenAI client initialized with fallback method")
            except Exception as e2:
                print(f"OpenAI fallback initialization error: {e2}")
                client = None
    else:
        print("Warning: OPENAI_API_KEY not found in environment variables or .env file")
        client = None
except Exception as e:
    print(f"OpenAI initialization error: {e}")
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

# ChatGPT message models
class ChatGPTMessage(BaseModel):
    role: str  # 'user', 'assistant', or 'system'
    content: str

class ChatGPTRequest(BaseModel):
    user_email: EmailStr
    messages: List[ChatGPTMessage]
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
# WebSocket connection manager for ChatGPT
class ChatGPTConnectionManager:
    def __init__(self):
        # Dictionary to store active connections by user_email
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, user_email: str):
        await websocket.accept()
        self.active_connections[user_email] = websocket
    
    def disconnect(self, user_email: str):
        if user_email in self.active_connections:
            del self.active_connections[user_email]
    
    async def send_message(self, message: str, user_email: str):
        if user_email in self.active_connections:
            await self.active_connections[user_email].send_text(message)
    
    def is_connected(self, user_email: str) -> bool:
        return user_email in self.active_connections

# Initialize connection manager
chatgpt_manager = ChatGPTConnectionManager()

# List of undergraduate majors
majors_doc = db.collection("majors").document("all_majors").get()
if majors_doc.exists:
   majors_data = majors_doc.to_dict()
   # All Majors
   majors = majors_data.get("majors", [])
   # Major Colors Map
   major_colors = majors_data.get("major_colors", {})
else:
   majors = []
   major_colors = {}
   print("Majors not found")
   print(f"# of majors: {len(majors)}")
   print(f"# of colors: {len(major_colors)}")


# Default university shorthand
user_university = "utd"  # Default to UTD (University of Texas at Dallas)

# categorize majors by school
cat_majors = db.collection("university").document(user_university).get()
if cat_majors.exists:
   cat_majors = cat_majors.to_dict()
   cat_majors = cat_majors["categorize_by_school"]
else:
   cat_majors = []
   print("Categorize majors by school not found")

# Load data function
def load_data():
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
        calendar_df = pd.read_csv("utd_events.csv") if os.path.exists("utd_events.csv") else None
        tutoring_df = pd.read_excel("UTD_tutoring.xlsx", engine="openpyxl") if os.path.exists("UTD_tutoring.xlsx") else None
        
        if activities_df is not None and 'List of Interests' in activities_df.columns:
            activities_df['List of Interests'] = activities_df['List of Interests'].apply(ast.literal_eval)
        
        print("Data loaded successfully")
        return activities_df, tutoring_df, orgs_df, events_df, courses_df, calendar_df
    except Exception as e:
        print(f"Error loading data: {e}")
        return None, None, None, None, None, None

#Load University Data
def university_data():
    try:
        # Check if data files exist
        data_files = [
            "CC_activities_ex.csv",
            "organizations_with_specific_majors.csv", 
            "filtered_utd_events_with_categories.csv",
            "utd_courses.csv",
            "utd_events.csv"
            "UTD_tutoring.xlsx",
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
        calendar_df = pd.read_csv("utd_events.csv") if os.path.exists("utd_events.csv") else None
        events_df = pd.read_csv("filtered_utd_events_with_categories.csv") if os.path.exists("filtered_utd_events_with_categories.csv") else None
        courses_df = pd.read_csv("utd_courses.csv") if os.path.exists("utd_courses.csv") else None
        tutoring_df = pd.read_excel("UTD_tutoring.xlsx", engine="openpyxl") if os.path.exists("UTD_tutoring.xlsx") else None
        
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
        fallback_message = "I'm sorry, but the AI service is currently unavailable. Please try again later or contact support."
        if stream:
            # For streaming, we need to create a mock stream
            class MockStream:
                async def __aiter__(self):
                    class MockChoice:
                        class MockDelta:
                            content = fallback_message
                        delta = MockDelta()
                    yield type('MockChunk', (), {'choices': [MockChoice()]})()  
            return MockStream()
        else:
            return fallback_message
    
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


async def stream_chatgpt_response(websocket: WebSocket, messages, user_email: str, model="gpt-4o-mini", temperature=0.7, max_tokens=150):
    """
    Stream a response from ChatGPT to a WebSocket connection
    """
    conversation_id = str(datetime.now().timestamp())
    full_response = ""
    
    try:
        # Get the streaming response
        stream = await generate_chatgpt_response(messages, model, temperature, max_tokens, stream=True)
        
        # Check if stream is valid
        if stream is None:
            await websocket.send_text("Error: Invalid stream object received. Please try again later.")
            return "Error: Invalid stream object", conversation_id
        
        # Process the stream
        try:
            chunk_count = 0
            start_time = datetime.now()
            
            # Stream each chunk to the WebSocket
            async for chunk in stream:
                chunk_count += 1
                
                try:
                    # Extract content from the chunk
                    content = _extract_content_from_chunk(chunk)
                    
                    # If content was extracted, send it to the client
                    if content:
                        full_response += content
                        await websocket.send_text(content)
                except Exception as chunk_error:
                    # Log the error but continue processing other chunks
                    print(f"Error processing chunk: {chunk_error}")
            
            # Log completion information
            duration = (datetime.now() - start_time).total_seconds()
            print(f"Streaming completed. Processed {chunk_count} chunks in {duration:.2f} seconds")
            
        except Exception as stream_error:
            # Handle streaming errors
            error_message = "Error while streaming response. Please try again later."
            await websocket.send_text(error_message)
            full_response += error_message
            
            # Send diagnostic info
            try:
                await websocket.send_text(f"\n\nDiagnostic info: {str(stream_error)[:100]}")
            except Exception:
                pass
        
        # Store conversation in Firestore if available
        await _store_conversation(db, user_email, messages, full_response, conversation_id, model)
        
        return full_response, conversation_id
        
    except Exception as e:
        error_message = f"Error preparing streaming response: {str(e)}"
        print(error_message)
        await websocket.send_text(error_message)
        return error_message, conversation_id

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
    return {"majors": majors}

@app.get("/categories")
async def get_categories():
    return {"categories": options}

@app.get("/major-colors")
async def get_major_colors():
    return {"major_colors": major_colors}

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
    
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")
    
    try:
        # Get user data
        user_doc = db.collection('users').document(request.user_email).get()
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
        interests = user_data.get('interests', [])
        academic_difficulty = user_data.get('academic_difficulty', 'Moderate')
        stress_level = user_data.get('stress_level', 'Low')
        satisfaction = user_data.get('satisfaction', 'Neutral')
        self_efficacy = user_data.get('self_efficacy', 'Moderate')
        financial_factors = user_data.get('financial_factors', 'N/A')
        family_responsibilities = user_data.get('family_responsibilities', 'N/A')
        outside_encouragement = user_data.get('outside_encouragement', [])
        
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

                if year == "1" and row["Category"] in ["Cultural", "Social", "Recreation"]:
                    score += social_support_rating/3
                    explanation_parts.append("This activity is ideal for first-year students to connect socially.")
                elif year in ["3", "4", "5+"] and row["Category"] in ["Academic Interests", "Educational/Departmental"]:
                    score += 1
                    explanation_parts.append("This activity provides valuable educational and departmental experience for upper-year students.")

                matched_interests = any(interest in row["Category"] for interest in interests)
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
   doc_ref = db.collection("university").document(univ.short_hand.lower())
   if doc_ref.get().exists:
       raise HTTPException(status_code=400, detail="University already exists")
   doc_ref.set(univ.dict())
   return {"message": "University created successfully"}


# Get a unviersity by name
@app.get("/universities/{short_hand}")
def get_university(short_hand: str):
   doc_ref = db.collection("university").document(short_hand.lower())
   if not doc_ref.get().exists:
       raise HTTPException(status_code=404, detail="University not found")
   return doc_ref.get().to_dict()


# Update a university
@app.put("/universities/{short_hand}")
def update_university(short_hand: str, univ: UniversityModel):
   doc_ref = db.collection("university").document(short_hand.lower())
   if not doc_ref.get().exists:
       raise HTTPException(status_code=404, detail="University not found")
   doc_ref.update(univ.dict())
   return {"message": "University updated successfully"}


# Delete a university
@app.delete("/universities/{short_hand}")
def delete_university(short_hand: str):
   doc_ref = db.collection("university").document(short_hand.lower())
   if not doc_ref.get().exists:
       raise HTTPException(status_code=404, detail="University not found")
   doc_ref.delete()
   return {"message": "University deleted successfully"}


# List all universities
@app.get("/universities")
def list_universities():
   universities = db.collection("university").get()
   return [university.to_dict() for university in universities]
  

# ChatGPT API Endpoints

@app.websocket("/ws/chatgpt/{user_email}")
async def chatgpt_websocket(websocket: WebSocket, user_email: str):
    """
    WebSocket endpoint for streaming ChatGPT responses
    """
    try:
        # Connect to the WebSocket
        await chatgpt_manager.connect(websocket, user_email)
        
        # Process messages
        while True:
            # Wait for a message from the client
            data = await websocket.receive_text()
            
            # Parse the message
            try:
                message_data = json.loads(data)
                
                # Create ChatGPT messages
                messages = []
                if "system" in message_data and message_data["system"]:
                    messages.append(ChatGPTMessage(role="system", content=message_data["system"]))
                
                # Add user message
                if "message" in message_data and message_data["message"]:
                    messages.append(ChatGPTMessage(role="user", content=message_data["message"]))
                else:
                    await websocket.send_text("Error: No message provided")
                    continue
                
                # Get model parameters
                model = message_data.get("model", "gpt-4o-mini")
                temperature = message_data.get("temperature", 0.7)
                max_tokens = message_data.get("max_tokens", 150)
                
                # Stream the response
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
        # Handle disconnection
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
    if calendar_df is None:
        raise HTTPException(status_code=500, detail="Calendar data not available")
    
    # Create a copy of the dataframe to avoid modifying the original
    filtered_events = calendar_df.copy()
    
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
