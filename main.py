from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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

class UserSignIn(BaseModel):
    email: EmailStr

class TokenVerification(BaseModel):
    token: str

class RecommendationRequest(BaseModel):
    user_email: EmailStr
    category: str = "orgs"  # orgs, events, tutoring

# List of undergraduate majors offered at UTD
majors = [
    "Accounting", "Actuarial Science", "American Studies", "Animation and Games",
    "Applied Cognition and Neuroscience", "Arts, Technology, and Emerging Communication",
    "Biochemistry", "Biology", "Biomedical Engineering", "Business Administration",
    "Business Analytics", "Chemistry", "Child Learning and Development", "Cognitive Science",
    "Computer Engineering", "Computer Science", "Criminology", "Data Science",
    "Economics", "Electrical Engineering", "Finance", "Geospatial Information Sciences",
    "Global Business", "Healthcare Management", "History", "Information Technology and Systems",
    "Interdisciplinary Studies", "International Political Economy", "Literature", "Marketing",
    "Mathematics", "Mechanical Engineering", "Molecular Biology", "Neuroscience",
    "Philosophy", "Physics", "Political Science", "Psychology", "Public Affairs",
    "Public Policy", "Sociology", "Software Engineering", "Speech, Language, and Hearing Sciences",
    "Supply Chain Management", "Visual and Performing Arts"
]

# UTD majors by school
utd_majors = {
    "School of Arts, Humanities, and Technology": [
        "Literature", "History", "Philosophy", "Art", "Communication", "Media",
        "Visual Arts", "Music", "Film", "Design", "Creative Writing", "Theater",
        "Humanities", "Cultural Studies", "Technology in Arts"
    ],
    "School of Behavioral and Brain Sciences": [
        "Psychology", "Neuroscience", "Cognitive Science", "Speech-Language Pathology",
        "Communication Disorders", "Counseling", "Behavioral Sciences"
    ],
    "Erik Jonsson School of Engineering and Computer Science": [
        "Computer Science", "Computer Engineering", "Electrical Engineering", "Mechanical Engineering",
        "Software Engineering", "Bioengineering", "Data Science", "Systems Engineering",
        "Cybersecurity", "Robotics"
    ],
    "School of Economic, Political and Policy Sciences": [
        "Economics", "Political Science", "Public Policy", "Criminology", "Sociology",
        "Policy Analysis", "Law", "Justice Studies", "Government", "Urban Planning",
        "International Relations", "Public Administration"
    ],
    "School of Interdisciplinary Studies": [
        "Interdisciplinary Studies", "General Studies", "Individualized Studies"
    ],
    "Naveen Jindal School of Management": [
        "Accounting", "Finance", "Marketing", "Business Administration", "Entrepreneurship",
        "Management", "Supply Chain Management", "Business Analytics", "Operations Management",
        "Strategy", "Investment", "Organizational Behavior", "Consulting", "Leadership"
    ],
    "School of Natural Sciences and Mathematics": [
        "Biology", "Chemistry", "Biochemistry", "Mathematics", "Physics", "Geosciences",
        "Environmental Science", "Ecology", "Genetics", "Cell Biology", "Statistics", "Science Education"
    ]
}

# Major colors mapping
major_colors = {
    "Literature": "#f94144", "History": "#f3722c", "Philosophy": "#f8961e", "Art": "#f9844a",
    "Communication": "#f9c74f", "Media": "#90be6d", "Visual Arts": "#43aa8b", "Music": "#4d908e",
    "Film": "#577590", "Design": "#277da1", "Creative Writing": "#7209b7", "Theater": "#3a0ca3",
    "Humanities": "#4361ee", "Cultural Studies": "#4895ef", "Technology in Arts": "#4cc9f0",
    "Psychology": "#ef476f", "Neuroscience": "#ffd166", "Cognitive Science": "#06d6a0",
    "Speech-Language Pathology": "#118ab2", "Communication Disorders": "#073b4c",
    "Counseling": "#ff8c42", "Behavioral Sciences": "#56ab91", "Computer Science": "#f4a261",
    "Computer Engineering": "#2a9d8f", "Electrical Engineering": "#264653", "Mechanical Engineering": "#e76f51",
    "Software Engineering": "#ffa69e", "Bioengineering": "#ff6b6b", "Data Science": "#6a0572",
    "Systems Engineering": "#3c1874", "Cybersecurity": "#5d8233", "Robotics": "#27aeef",
    "Economics": "#1d3557", "Political Science": "#457b9d", "Public Policy": "#a8dadc",
    "Criminology": "#e63946", "Sociology": "#f1faee", "Policy Analysis": "#bc6c25",
    "Law": "#fefae0", "Justice Studies": "#606c38", "Government": "#283618",
    "Urban Planning": "#dda15e", "International Relations": "#a44a3f", "Public Administration": "#80b918",
    "Interdisciplinary Studies": "#1f4e5f", "General Studies": "#41b3a3", "Individualized Studies": "#85c7f2",
    "Accounting": "#ffbe0b", "Finance": "#fb5607", "Marketing": "#ff006e", "Business Administration": "#8338ec",
    "Entrepreneurship": "#3a86ff", "Management": "#02c39a", "Supply Chain Management": "#00a896",
    "Business Analytics": "#028090", "Operations Management": "#05668d", "Strategy": "#adc178",
    "Investment": "#ddbea9", "Organizational Behavior": "#ffe8d6", "Consulting": "#cb997e",
    "Leadership": "#a5a58d", "Biology": "#ff595e", "Chemistry": "#ffca3a", "Biochemistry": "#8ac926",
    "Mathematics": "#1982c4", "Physics": "#6a4c93", "Geosciences": "#6d597a", "Environmental Science": "#ffe066",
    "Ecology": "#dddf00", "Genetics": "#1b998b", "Cell Biology": "#c32f27", "Statistics": "#2d6a4f",
    "Science Education": "#a9def9"
}

# Load data function
def load_data():
    try:
        # Check if data files exist
        data_files = [
            "CC_activities_ex.csv",
            "organizations_with_specific_majors.csv", 
            "filtered_utd_events_with_categories.csv",
            "utd_courses.csv",
            "UTD_tutoring.xlsx"
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
        
        if activities_df is not None and 'List of Interests' in activities_df.columns:
            activities_df['List of Interests'] = activities_df['List of Interests'].apply(ast.literal_eval)
        
        print("Data loaded successfully")
        return activities_df, tutoring_df, orgs_df, events_df, courses_df
    except Exception as e:
        print(f"Error loading data: {e}")
        return None, None, None, None, None

# Load data at startup
activities_df, tutoring_df, orgs_df, events_df, courses_df = load_data()

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
