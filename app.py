from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import pandas as pd
from urllib.parse import urlparse
import ast
import json
import re
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, auth, firestore
from flask_cors import CORS

# Path to the Firebase service account key file
FIREBASE_KEY_PATH = "firebase-key.json"



app = Flask(__name__)
app.secret_key = 'your_secret_key'

CORS(app)

# Initialize Firebase Admin SDK
cred = credentials.Certificate("firebase-key.json")  # Replace with your Firebase Admin SDK JSON file
firebase_admin.initialize_app(cred)


# Initialize Firestore
db = firestore.client()

# List of undergraduate majors offered at UTD
majors = [
    "Accounting",
    "Actuarial Science",
    "American Studies",
    "Animation and Games",
    "Applied Cognition and Neuroscience",
    "Arts, Technology, and Emerging Communication",
    "Biochemistry",
    "Biology",
    "Biomedical Engineering",
    "Business Administration",
    "Business Analytics",
    "Chemistry",
    "Child Learning and Development",
    "Cognitive Science",
    "Computer Engineering",
    "Computer Science",
    "Criminology",
    "Data Science",
    "Economics",
    "Electrical Engineering",
    "Finance",
    "Geospatial Information Sciences",
    "Global Business",
    "Healthcare Management",
    "History",
    "Information Technology and Systems",
    "Interdisciplinary Studies",
    "International Political Economy",
    "Literature",
    "Marketing",
    "Mathematics",
    "Mechanical Engineering",
    "Molecular Biology",
    "Neuroscience",
    "Philosophy",
    "Physics",
    "Political Science",
    "Psychology",
    "Public Affairs",
    "Public Policy",
    "Sociology",
    "Software Engineering",
    "Speech, Language, and Hearing Sciences",
    "Supply Chain Management",
    "Visual and Performing Arts"
]

# Define a function to load the example activities data
def load_data():
    # Load CSV files
    activities_df = pd.read_csv("CC_activities_ex.csv")
    orgs_df = pd.read_csv("organizations_with_specific_majors.csv")
    events_df = pd.read_csv("filtered_utd_events_with_categories.csv")
    courses_df = pd.read_csv("utd_courses.csv")

    # Load Excel file
    tutoring_df = pd.read_excel("UTD_tutoring.xlsx", engine="openpyxl")
    
    # Convert 'List of Interests' column from string to list
    activities_df['List of Interests'] = activities_df['List of Interests'].apply(ast.literal_eval)
    
    return activities_df, tutoring_df, orgs_df, events_df, courses_df

# Load data at the start of the application
activities_df, tutoring_df, orgs_df, events_df, courses_df = load_data()

categories = orgs_df['Category'].dropna().unique()
cleaned_categories = []

for category in categories:
    # Remove unwanted characters like brackets and quotes
    category = re.sub(r'[\[\]"]', '', category).strip()
    # Split by commas, semicolons, and periods, then add each item to the list
    cleaned_categories.extend([item.strip() for item in re.split(r'[;,\.]', category)])

# Remove duplicates and sort
options = sorted(set(cleaned_categories))

utd_majors = {
    "School of Arts, Humanities, and Technology": [
        "Literature",
        "History",
        "Philosophy",
        "Art",
        "Communication",
        "Media",
        "Visual Arts",
        "Music",
        "Film",
        "Design",
        "Creative Writing",
        "Theater",
        "Humanities",
        "Cultural Studies",
        "Technology in Arts"
    ],
    "School of Behavioral and Brain Sciences": [
        "Psychology",
        "Neuroscience",
        "Cognitive Science",
        "Speech-Language Pathology",
        "Communication Disorders",
        "Counseling",
        "Behavioral Sciences"
    ],
    "Erik Jonsson School of Engineering and Computer Science": [
        "Computer Science",
        "Computer Engineering",
        "Electrical Engineering",
        "Mechanical Engineering",
        "Software Engineering",
        "Bioengineering",
        "Data Science",
        "Systems Engineering",
        "Cybersecurity",
        "Robotics"
    ],
    "School of Economic, Political and Policy Sciences": [
        "Economics",
        "Political Science",
        "Public Policy",
        "Criminology",
        "Sociology",
        "Policy Analysis",
        "Law",
        "Justice Studies",
        "Government",
        "Urban Planning",
        "International Relations",
        "Public Administration"
    ],
    "School of Interdisciplinary Studies": [
        "Interdisciplinary Studies",
        "General Studies",
        "Individualized Studies"
    ],
    "Naveen Jindal School of Management": [
        "Accounting",
        "Finance",
        "Marketing",
        "Business Administration",
        "Entrepreneurship",
        "Management",
        "Supply Chain Management",
        "Business Analytics",
        "Operations Management",
        "Strategy",
        "Investment",
        "Organizational Behavior",
        "Consulting",
        "Leadership"
    ],
    "School of Natural Sciences and Mathematics": [
        "Biology",
        "Chemistry",
        "Biochemistry",
        "Mathematics",
        "Physics",
        "Geosciences",
        "Environmental Science",
        "Ecology",
        "Genetics",
        "Cell Biology",
        "Statistics",
        "Science Education"
    ]
}

# Assign specific colors to majors
major_colors = {
    "Literature": "#f94144",
    "History": "#f3722c",
    "Philosophy": "#f8961e",
    "Art": "#f9844a",
    "Communication": "#f9c74f",
    "Media": "#90be6d",
    "Visual Arts": "#43aa8b",
    "Music": "#4d908e",
    "Film": "#577590",
    "Design": "#277da1",
    "Creative Writing": "#7209b7",
    "Theater": "#3a0ca3",
    "Humanities": "#4361ee",
    "Cultural Studies": "#4895ef",
    "Technology in Arts": "#4cc9f0",
    
    "Psychology": "#ef476f",
    "Neuroscience": "#ffd166",
    "Cognitive Science": "#06d6a0",
    "Speech-Language Pathology": "#118ab2",
    "Communication Disorders": "#073b4c",
    "Counseling": "#ff8c42",
    "Behavioral Sciences": "#56ab91",
    
    "Computer Science": "#f4a261",
    "Computer Engineering": "#2a9d8f",
    "Electrical Engineering": "#264653",
    "Mechanical Engineering": "#e76f51",
    "Software Engineering": "#ffa69e",
    "Bioengineering": "#ff6b6b",
    "Data Science": "#6a0572",
    "Systems Engineering": "#3c1874",
    "Cybersecurity": "#5d8233",
    "Robotics": "#27aeef",
    
    "Economics": "#1d3557",
    "Political Science": "#457b9d",
    "Public Policy": "#a8dadc",
    "Criminology": "#e63946",
    "Sociology": "#f1faee",
    "Policy Analysis": "#bc6c25",
    "Law": "#fefae0",
    "Justice Studies": "#606c38",
    "Government": "#283618",
    "Urban Planning": "#dda15e",
    "International Relations": "#a44a3f",
    "Public Administration": "#80b918",
    
    "Interdisciplinary Studies": "#1f4e5f",
    "General Studies": "#41b3a3",
    "Individualized Studies": "#85c7f2",
    
    "Accounting": "#ffbe0b",
    "Finance": "#fb5607",
    "Marketing": "#ff006e",
    "Business Administration": "#8338ec",
    "Entrepreneurship": "#3a86ff",
    "Management": "#02c39a",
    "Supply Chain Management": "#00a896",
    "Business Analytics": "#028090",
    "Operations Management": "#05668d",
    "Strategy": "#adc178",
    "Investment": "#ddbea9",
    "Organizational Behavior": "#ffe8d6",
    "Consulting": "#cb997e",
    "Leadership": "#a5a58d",
    
    "Biology": "#ff595e",
    "Chemistry": "#ffca3a",
    "Biochemistry": "#8ac926",
    "Mathematics": "#1982c4",
    "Physics": "#6a4c93",
    "Geosciences": "#6d597a",
    "Environmental Science": "#ffe066",
    "Ecology": "#dddf00",
    "Genetics": "#1b998b",
    "Cell Biology": "#c32f27",
    "Statistics": "#2d6a4f",
    "Science Education": "#a9def9",
}

# Function to identify the college based on the major
def get_college_by_major(major):
    for college, majors in utd_majors.items():
        if major in majors:
            return college
    return "Any College"

def format_datetime(iso_string):
    if not isinstance(iso_string, str) or not iso_string.strip():
        return "Time Not Found"  # Fallback for invalid inputs
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%A, %B %d, %Y, %I:%M %p")
    except ValueError:
        return iso_string  # Return the original string if formatting fails

def extract_event_name(url):
    # Parse the URL and get the path
    path = urlparse(url).path
    
    # Extract the event name from the path (assuming it comes after "event/")
    event_name = path.split("/event/")[-1] if "/event/" in path else ""
    
    # Replace hyphens with spaces and capitalize the words for a clean display
    event_name = event_name.replace("-", " ").title()
    
    return event_name
 
@app.route('/')
def default_route():
    return redirect('/signup', code=302)

# Route to serve the login page
@app.route("/home")
def home():
    return render_template("index.html")


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        # Collect form data
        user_data = {
            'name': request.form['name'],
            'surname': request.form['surname'],
            'school_name': request.form['school_name'],
            'year': request.form['year'],
            'ftcs_status': request.form['ftcs_status'],
            'gpa_range': request.form['gpa_range'],
            'educational_goals': request.form['educational_goals'],
            'age': request.form['age'],
            'gender': request.form['gender'],
            'race_ethnicity': request.form['race_ethnicity'],
            'working_hours': request.form['working_hours'],
            'stress_level': request.form['stress_level'],
            'self_efficacy': request.form['self_efficacy'],
            'major': request.form['major'],
            'interests': json.loads(request.form['interests']) if 'interests' in request.form else [],
            'email': request.form['email']
        }

        # Use the user's email as the unique identifier
        user_email = user_data.get('email')

        if not user_email:
            return "Error: Email is required to sign up.", 400

        # Save the user data to Firestore
        try:
            db.collection('users').document(user_email).set(user_data)
            print(f"User data saved to Firestore: {user_data}")
        except Exception as e:
            return f"An error occurred while saving data: {str(e)}", 500

        # Save the user data in the session
        session['user_data'] = user_data
        session['logged_in'] = True

        # Redirect to the recommendations page
        return redirect(url_for('recommendations', email=user_email))

    # Render the signup form
    return render_template('signup.html', majors=majors, options=options)

@app.route('/signin', methods=['GET', 'POST'])
def signin():
    if request.method == 'GET':
        # Render the sign-in page
        return render_template('signin.html')

    if request.method == 'POST':
        try:
            # Parse JSON from the request
            data = request.get_json()
            print("Incoming data:", data)

            # Extract the email
            user_email = data.get('email')
            if not user_email:
                return "Error: Email is required to sign in.", 400

            # Check if user exists in Firestore
            user_doc = db.collection('users').document(user_email).get()
            if user_doc.exists:
                # Save user data to the session
                session['user_data'] = user_doc.to_dict()
                session['logged_in'] = True
                print(f"User signed in: {session['user_data']}")
                return "Sign-in successful.", 200
            else:
                return "Error: User not found. Please sign up first.", 404
        except Exception as e:
            print("Error during sign-in:", str(e))
            return f"An error occurred while signing in: {str(e)}", 500

@app.route('/verify-token', methods=['POST'])
def verify_token():
    try:
        token = request.json.get('token')
        decoded_token = auth.verify_id_token(token)
        user_email = decoded_token.get("email")
        return jsonify({"user": user_email}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 401

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    # Redirect to login if the user is not logged in
    if not session.get('logged_in'):
        return redirect(url_for('signup'))
    
    # Get the user's email from session data
    user_email = session.get('user_data', {}).get('email')
    if not user_email:
        return redirect(url_for('signup'))  # Redirect to login if email is missing
    
    if request.method == 'POST':
        # Collect form data and update in Firestore
        updated_user_data = {
            'name': request.form['name'],
            'surname': request.form['surname'],
            'school_name': request.form['school_name'],
            'year': request.form['year'],
            'ftcs_status': request.form['ftcs_status'],
            'gpa_range': request.form['gpa_range'],
            'high_school_grades': request.form['high_school_grades'],
            'educational_goals': request.form['educational_goals'],
            'age': request.form['age'],
            'gender': request.form['gender'],
            'race_ethnicity': request.form['race_ethnicity'],
            'financial_factors': request.form['financial_factors'],
            'working_hours': request.form['working_hours'],
            'family_responsibilities': request.form['family_responsibilities'],
            'outside_encouragement': request.form['outside_encouragement'],
            'opportunity_to_transfer': request.form['opportunity_to_transfer'],
            'current_gpa': request.form['current_gpa'],
            'academic_difficulty': request.form['academic_difficulty'],
            'stress_level': request.form['stress_level'],
            'satisfaction': request.form['satisfaction'],
            'self_efficacy': request.form['self_efficacy'],
            'major': request.form['major'],
            'interests': request.form.getlist('interests')  # For multiple select fields
        }

        try:
            # Update data in Firestore
            db.collection('users').document(user_email).update(updated_user_data)

            # Update the session data
            session['user_data'] = updated_user_data
            print(f"User data updated: {updated_user_data}")
        except Exception as e:
            return f"An error occurred while updating data: {str(e)}", 500

        return redirect(url_for('profile'))

    # Retrieve user data from Firestore if session data is missing
    if not session.get('user_data'):
        try:
            user_doc = db.collection('users').document(user_email).get()
            if user_doc.exists:
                session['user_data'] = user_doc.to_dict()
            else:
                return "Error: User data not found.", 404
        except Exception as e:
            return f"An error occurred while retrieving data: {str(e)}", 500

    user_data = session.get('user_data', {})
    return render_template('profile.html', user_data=user_data, majors=majors, options=options)

@app.route('/page1')
def page1():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('scholarship.html')

@app.route('/page2')
def page2():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    # If dynamic content is needed, you can pass data to the template
    # Collect all user inputs from the form and store them in session as individual variables
    
    # Retrieve user_data from the session
    user_data = session.get('user_data', {})

    # Extract individual variables from user_data
    name = user_data.get('name', 'Unknown')
    surname = user_data.get('surname', 'Unknown')
    school_name = user_data.get('school_name', 'Unknown')
    year = user_data.get('year', '1')
    ftcs_status = user_data.get('ftcs_status', 'No')
    gpa_range = user_data.get('gpa_range', '<2.0')
    high_school_grades = user_data.get('high_school_grades', 'N/A')
    educational_goals = user_data.get('educational_goals', 'N/A')
    age = user_data.get('age', '18')
    gender = user_data.get('gender', 'Unknown')
    race_ethnicity = user_data.get('race_ethnicity', 'Unknown')
    financial_factors = user_data.get('financial_factors', 'N/A')
    working_hours = user_data.get('working_hours', '0')
    family_responsibilities = user_data.get('family_responsibilities', 'N/A')
    outside_encouragement = user_data.get('outside_encouragement', [])
    opportunity_to_transfer = user_data.get('opportunity_to_transfer', 'No')
    current_gpa = user_data.get('current_gpa', '0.0')
    academic_difficulty = user_data.get('academic_difficulty', 'Moderate')
    stress_level = user_data.get('stress_level', 'Low')
    satisfaction = user_data.get('satisfaction', 'Neutral')
    self_efficacy = user_data.get('self_efficacy', 'Moderate')
    major = user_data.get('major', 'Undeclared')
    interests = user_data.get('interests', 'N/A')
    
    # Identify the college based on the selected major
    selected_college = get_college_by_major(major)

    def calculate_social_support():
        score = 0
        if academic_difficulty == "Difficult" or stress_level == "High":
            score += 2
        if "Peers" in outside_encouragement or "Community" in outside_encouragement:
            score -= 1  # Having peer or community support reduces the need
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
        if "Teachers" in outside_encouragement:  # Support from teachers reduces the need
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
        if "Family" in outside_encouragement:  # Family encouragement reduces the need
            score -= 1
        return min(max(score, 1), 5)

    # Calculate Ratings
    social_support_rating = calculate_social_support()
    intellectual_support_rating = calculate_intellectual_support()
    career_development_rating = calculate_career_development()

    def score_events(df, school_name, year, ftcs_status, gpa_range, major, interests):
        # Define mappings from school names to categories they influence
        school_category_mappings = {
            "School of Arts, Humanities, and Technology": "Social",
            "Naveen Jindal School of Management": "Business",
            "Erik Jonsson School of Engineering and Computer Science": "STEM",
            "School of Natural Sciences and Mathematics": "STEM",
            "School of Behavioral and Brain Sciences": "STEM",
            "School of Economic, Political and Policy Sciences": "Business"
        }

        # Get the school based on the major (assuming get_college_by_major is a function that determines this)
        school = get_college_by_major(major)
        
        # Initialize lists for scores and explanations
        scores = []
        explanations = []

        for _, row in df.iterrows():
            # Initialize cumulative score and explanation parts for this row
            score = 0
            explanation_parts = []

            # Year-based scoring for Social
            if year == "1":
                score += social_support_rating/3
                explanation_parts.append("Social events can help first-year students build a network and feel more connected to the campus community.")

            # GPA-based scoring for Tutoring
            if gpa_range == '<2.0' or gpa_range == '2.0 - 2.5':
                score += intellectual_support_rating/3
                explanation_parts.append("With a GPA below 2.5, tutoring is highly recommended to support your academic growth.")

            # Year-based scoring for Career Development/Honors
            if year in ["3", "4", "5"]:
                score += career_development_rating/3
                explanation_parts.append("As a 3rd, 4th, or 5th-year student, career development opportunities can help you prepare for post-graduation goals.")

            # School-based scoring
            if school in school_category_mappings:
                category = school_category_mappings[school]
                score += 1
                explanation_parts.append(f"Being in the {school} makes this opportunity more relevant for {category}.")

            # FTC status scoring for Social
            if ftcs_status:
                score += 1
                explanation_parts.append("As an FTC student, social events can help you integrate and feel more connected to the community.")

            # Append cumulative score and combined explanation
            score = round(score, 2)
            scores.append(score)
            explanations.append(" ".join(explanation_parts))

        # Assign the cumulative scores and explanations to new columns in the DataFrame
        df["Score"] = scores
        df["Recommendation Explanation"] = explanations
        
        # Sort by the cumulative score in descending order and keep only the top 7 results
        top_results = df.sort_values(by="Score", ascending=False).head(7)

        return top_results

    def extract_event_name(url):
        # Parse the URL and get the path
        path = urlparse(url).path
        
        # Split the path to get the event name part
        event_name = path.split("/event/")[-1]
        
        # Replace hyphens with spaces and capitalize each word
        event_name = event_name.replace("-", " ").title()
        
        return event_name

    # Dictionary mapping each college to its advising URL
    advising_links = {
        "School of Arts, Humanities, and Technology": "https://bass.utdallas.edu/undergraduate-advising/",
        "School of Behavioral and Brain Sciences": "https://bbs.utdallas.edu/advising/",
        "Erik Jonsson School of Engineering and Computer Science": "https://engineering.utdallas.edu/academics/undergraduate-majors/undergrad-advising/",
        "School of Economic, Political and Policy Sciences": "https://epps.utdallas.edu/current-students/undergraduate-advising/",
        "School of Interdisciplinary Studies": "https://is.utdallas.edu/contact/advisors/",
        "Naveen Jindal School of Management": "https://jindal.utdallas.edu/advising/",
        "School of Natural Sciences and Mathematics": "https://nsm.utdallas.edu/advising/",
        "Any College": "https://oue.utdallas.edu/undergraduate-advising/"
    }

    # Function to get the advising link based on major
    def get_advising_link(major):
        college = get_college_by_major(major)  # This function already determines the college based on the major
        return advising_links.get(college, "https://oue.utdallas.edu/undergraduate-advising/")  # Default to general advising link if not found

    advising_link = get_advising_link(major)

    scored_events = score_events(events_df, school_name, year, ftcs_status, gpa_range, major, interests)

    recommendations = scored_events.to_dict(orient='records')
    for item in recommendations:
        item['Formatted Start Time'] = format_datetime(item['Start Time'])
        item['Formatted End Time'] = format_datetime(item['End Time'])
        item['Event Name'] = extract_event_name(item.get('URL', ''))  # Extract event name from URL

    return render_template(
        'calender.html',
        recommendations=recommendations
        )

@app.route('/recommendations')
def recommendations():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    # If dynamic content is needed, you can pass data to the template
    # Collect all user inputs from the form and store them in session as individual variables
    
    # Retrieve user_data from the session
    user_data = session.get('user_data', {})

    # Extract individual variables from user_data
    name = user_data.get('name', 'Unknown')
    surname = user_data.get('surname', 'Unknown')
    school_name = user_data.get('school_name', 'Unknown')
    year = user_data.get('year', '1')
    ftcs_status = user_data.get('ftcs_status', 'No')
    gpa_range = user_data.get('gpa_range', '<2.0')
    high_school_grades = user_data.get('high_school_grades', 'N/A')
    educational_goals = user_data.get('educational_goals', 'N/A')
    age = user_data.get('age', '18')
    gender = user_data.get('gender', 'Unknown')
    race_ethnicity = user_data.get('race_ethnicity', 'Unknown')
    financial_factors = user_data.get('financial_factors', 'N/A')
    working_hours = user_data.get('working_hours', '0')
    family_responsibilities = user_data.get('family_responsibilities', 'N/A')
    outside_encouragement = user_data.get('outside_encouragement', [])
    opportunity_to_transfer = user_data.get('opportunity_to_transfer', 'No')
    current_gpa = user_data.get('current_gpa', '0.0')
    academic_difficulty = user_data.get('academic_difficulty', 'Moderate')
    stress_level = user_data.get('stress_level', 'Low')
    satisfaction = user_data.get('satisfaction', 'Neutral')
    self_efficacy = user_data.get('self_efficacy', 'Moderate')
    major = user_data.get('major', 'Undeclared')
    interests = user_data.get('interests', 'N/A')
    
    # Identify the college based on the selected major
    selected_college = get_college_by_major(major)

    def calculate_social_support():
        score = 0
        if academic_difficulty == "Difficult" or stress_level == "High":
            score += 2
        if "Peers" in outside_encouragement or "Community" in outside_encouragement:
            score -= 1  # Having peer or community support reduces the need
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
        if "Teachers" in outside_encouragement:  # Support from teachers reduces the need
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
        if "Family" in outside_encouragement:  # Family encouragement reduces the need
            score -= 1
        return min(max(score, 1), 5)

    # Calculate Ratings
    social_support_rating = calculate_social_support()
    intellectual_support_rating = calculate_intellectual_support()
    career_development_rating = calculate_career_development()

    # Scoring function with recommendation explanation
    def score_orgs(df, school_name, year, ftcs_status, gpa_range, major, interests):
        # Determine user's college based on major
        user_college = get_college_by_major(major)
        
        scores = []
        explanations = []
        
        for _, row in df.iterrows():
            score = 0
            explanation_parts = []

            # Parse the "Specific Majors" column from string to list
            specific_majors = ast.literal_eval(row["Specific Majors"]) if row["Specific Majors"] else []

            # Score based on year and category
            if year == "1" and row["Category"] in ["Cultural", "Social", "Recreation"]:
                score += social_support_rating/3
                explanation_parts.append("This activity is ideal for first-year students to connect socially.")
            elif year in ["3", "4", "5+"] and row["Category"] in ["Academic Interests", "Educational/Departmental"]:
                score += 1
                explanation_parts.append("This activity provides valuable educational and departmental experience for upper-year students.")

            # Score based on interests matching in both Interests and Category columns
            matched_interests = any(
                interest in row["Category"] for interest in interests
            )
            
            if matched_interests:
                score += 1
                explanation_parts.append("This activity aligns with your interests.")
            
            # Score based on college match
            if row["Majors"] == user_college:
                score += 2
                explanation_parts.append("This activity is relevant to your college.")
            
            if row["Majors"] == 'any major':
                score += .5
                explanation_parts.append("This activity is open to all majors.")
            
            # Score based on exact major match
            if major in specific_majors:
                score += 3
                explanation_parts.append("This activity directly aligns with your major.")

            # Append score and explanation for each activity
            score = round(score, 2)
            scores.append(score)
            explanations.append(" ".join(explanation_parts))

        # Add scores and explanations to the DataFrame and sort by Score
        df["Score"] = scores
        df["Recommendation Explanation"] = explanations
        # Sort by the cumulative score in descending order and keep only the top 7 results
        top_results = df.sort_values(by="Score", ascending=False).head(7)

        return top_results

    # Scoring function for tutoring 
    def score_tutoring(df, school_name, year, ftcs_status, gpa_range, major, interests):
        scores = []
        explanations = []
        
        for _, row in df.iterrows():
            score = 0
            explanation_parts = []

            # # Parse "Majors" field if it exists
            try:
                majors = row['Majors']
                majors = majors.split(", ")
                # print(majors)

            except (ValueError, SyntaxError):
                majors = "All Majors"
            
            if major not in majors and majors != ['All Majors']:
                scores.append(0)
                explanations.append('This opportunity may not be for your major')
                continue

            # Major-based scoring
            if major in majors: 
                score += 1
                explanation_parts.append("This opportunity is perfect for your major!")

            # Year-based scoring
            if year == "1":
                score += 2
                explanation_parts.append("Tutoring can be a fantastic resource for first-year students adapting to the college workload!")
            elif year == "2":
                score += 1
                explanation_parts.append("Tutoring is highly beneficial for underclassmen building a strong academic foundation.")

            # GPA-based recommendations
            if gpa_range == '<2.0' or gpa_range == '2.0 - 2.5':
                score += intellectual_support_rating/3
                explanation_parts.append("Tutoring can be a valuable tool to help you strengthen your academic performance and reach your goals!")
            elif gpa_range == '2.6 - 3.0':
                score += intellectual_support_rating/3
                explanation_parts.append("With a bit of extra support, you can build on your achievements and keep moving towards your academic potential.")
            elif gpa_range == '3.1 - 3.5':
                score += intellectual_support_rating/3
                explanation_parts.append("Tutoring can be a great way to maintain and even boost your already solid academic standing.")

            # Append score and explanation
            score = round(score, 2)
            scores.append(score)
            explanations.append(" ".join(explanation_parts))

        
        # Assign scores and explanations to the DataFrame
        df["Score"] = scores
        df["Recommendation Explanation"] = explanations
        return df.sort_values(by="Score", ascending=False)

    def score_events(df, school_name, year, ftcs_status, gpa_range, major, interests):
        # Define mappings from school names to categories they influence
        school_category_mappings = {
            "School of Arts, Humanities, and Technology": "Social",
            "Naveen Jindal School of Management": "Business",
            "Erik Jonsson School of Engineering and Computer Science": "STEM",
            "School of Natural Sciences and Mathematics": "STEM",
            "School of Behavioral and Brain Sciences": "STEM",
            "School of Economic, Political and Policy Sciences": "Business"
        }

        # Get the school based on the major (assuming get_college_by_major is a function that determines this)
        school = get_college_by_major(major)
        
        # Initialize lists for scores and explanations
        scores = []
        explanations = []

        for _, row in df.iterrows():
            # Initialize cumulative score and explanation parts for this row
            score = 0
            explanation_parts = []

            # Year-based scoring for Social
            if year == "1":
                score += social_support_rating/3
                explanation_parts.append("Social events can help first-year students build a network and feel more connected to the campus community.")

            # GPA-based scoring for Tutoring
            if gpa_range == '<2.0' or gpa_range == '2.0 - 2.5':
                score += intellectual_support_rating/3
                explanation_parts.append("With a GPA below 2.5, tutoring is highly recommended to support your academic growth.")

            # Year-based scoring for Career Development/Honors
            if year in ["3", "4", "5"]:
                score += career_development_rating/3
                explanation_parts.append("As a 3rd, 4th, or 5th-year student, career development opportunities can help you prepare for post-graduation goals.")

            # School-based scoring
            if school in school_category_mappings:
                category = school_category_mappings[school]
                score += 1
                explanation_parts.append(f"Being in the {school} makes this opportunity more relevant for {category}.")

            # FTC status scoring for Social
            if ftcs_status:
                score += 1
                explanation_parts.append("As an FTC student, social events can help you integrate and feel more connected to the community.")

            # Append cumulative score and combined explanation
            score = round(score, 2)
            scores.append(score)
            explanations.append(" ".join(explanation_parts))

        # Assign the cumulative scores and explanations to new columns in the DataFrame
        df["Score"] = scores
        df["Recommendation Explanation"] = explanations
        
        # Sort by the cumulative score in descending order and keep only the top 7 results
        top_results = df.sort_values(by="Score", ascending=False).head(7)

        return top_results

    def extract_event_name(url):
        # Parse the URL and get the path
        path = urlparse(url).path
        
        # Split the path to get the event name part
        event_name = path.split("/event/")[-1]
        
        # Replace hyphens with spaces and capitalize each word
        event_name = event_name.replace("-", " ").title()
        
        return event_name

    # Dictionary mapping each college to its advising URL
    advising_links = {
        "School of Arts, Humanities, and Technology": "https://bass.utdallas.edu/undergraduate-advising/",
        "School of Behavioral and Brain Sciences": "https://bbs.utdallas.edu/advising/",
        "Erik Jonsson School of Engineering and Computer Science": "https://engineering.utdallas.edu/academics/undergraduate-majors/undergrad-advising/",
        "School of Economic, Political and Policy Sciences": "https://epps.utdallas.edu/current-students/undergraduate-advising/",
        "School of Interdisciplinary Studies": "https://is.utdallas.edu/contact/advisors/",
        "Naveen Jindal School of Management": "https://jindal.utdallas.edu/advising/",
        "School of Natural Sciences and Mathematics": "https://nsm.utdallas.edu/advising/",
        "Any College": "https://oue.utdallas.edu/undergraduate-advising/"
    }

    # Function to get the advising link based on major
    def get_advising_link(major):
        college = get_college_by_major(major)  # This function already determines the college based on the major
        return advising_links.get(college, "https://oue.utdallas.edu/undergraduate-advising/")  # Default to general advising link if not found

    advising_link = get_advising_link(major)

    scored_df = score_orgs(orgs_df, school_name, year, ftcs_status, gpa_range, major, interests)

    scored_events = score_events(events_df, school_name, year, ftcs_status, gpa_range, major, interests)

    # st.dataframe(tutoring_df)
    scored_tutoring = score_tutoring(tutoring_df, school_name, year, ftcs_status, gpa_range, major, interests)
    
    
    # Default tab to display
    selected_category = request.args.get('category', 'orgs')

    # Filter the data based on the selected category
    if selected_category == 'orgs':
        recommendations = scored_df.to_dict(orient='records')
    elif selected_category == 'events':
        recommendations = scored_events.to_dict(orient='records')
        for item in recommendations:
            item['Formatted Start Time'] = format_datetime(item['Start Time'])
            item['Formatted End Time'] = format_datetime(item['End Time'])
            item['Event Name'] = extract_event_name(item.get('URL', ''))  # Extract event name from URL

    elif selected_category == 'tutoring':
        filtered_tutoring = scored_tutoring[scored_tutoring["Score"] > 0]
        recommendations = filtered_tutoring.to_dict(orient='records')
    else:
        recommendations = []

    return render_template(
        'recommendations.html',
        recommendations=recommendations,
        selected_category=selected_category, 
        major_colors=major_colors
    )

@app.route('/logout')
def logout():
    # Clear the session data
    session.clear()
    # Redirect to the sign-in page
    return redirect(url_for('signin'))

@app.route('/submit_review', methods=['POST'])
def submit_review():
    rating = request.form.get('rating')
    comments = request.form.get('comments')

    # Save or process the review
    print(f"Rating: {rating}, Comments: {comments}")

    # Flash a success message
    flash('Thank you for submitting your review!')

    # Redirect back to the referring page
    return redirect(request.referrer)

if __name__ == '__main__':
    app.run(debug=True)
