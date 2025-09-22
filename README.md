# Campus Connect Backend API

A FastAPI-based backend service for the Campus Connect application, providing personalized recommendations for students based on their academic profile and interests.

## Features

- **User Management**: Sign up, sign in, and profile management
- **Personalized Recommendations**: AI-powered recommendations for organizations, events, and tutoring
- **Firebase Integration**: User data storage and authentication
- **CORS Support**: Ready for frontend integration
- **Data Processing**: CSV/Excel data processing for recommendations

## Prerequisites

- Python 3.8+
- Firebase project with Firestore enabled
- Firebase service account key file

## Installation

1. **Clone the repository**
   ```bash
   cd CCBack
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up Firebase**
   - Create a Firebase project at https://console.firebase.google.com/
   - Enable Firestore Database
   - Generate a service account key
   - Download the JSON file and rename it to `firebase-key.json`
   - Place it in the `CCBack` directory

5. **Prepare data files**
   Place the following CSV/Excel files in the `CCBack` directory:
   - `CC_activities_ex.csv`
   - `organizations_with_specific_majors.csv`
   - `filtered_utd_events_with_categories.csv`
   - `utd_courses.csv`
   - `UTD_tutoring.xlsx`

## Running the Server

```bash
# Development server
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Production server
uvicorn main:app --host 0.0.0.0 --port 8000
```

The API will be available at:
- **Local**: http://localhost:8000
- **API Documentation**: http://localhost:8000/docs
- **Alternative Docs**: http://localhost:8000/redoc

## API Endpoints

### Authentication & User Management
- `POST /signup` - Create a new user account
- `POST /signin` - Sign in with email
- `POST /verify-token` - Verify Firebase token
- `GET /profile/{user_email}` - Get user profile
- `PUT /profile/{user_email}` - Update user profile

### Data & Configuration
- `GET /majors` - Get list of available majors
- `GET /categories` - Get organization categories
- `GET /major-colors` - Get major color mappings

### Recommendations
- `POST /recommendations` - Get personalized recommendations
  - Categories: `orgs`, `events`, `tutoring`

### Health Check
- `GET /` - API information
- `GET /health` - Health check endpoint

## Environment Variables

Create a `.env` file in the `CCBack` directory:

```env
# Firebase Configuration
FIREBASE_PROJECT_ID=your-project-id
FIREBASE_PRIVATE_KEY_ID=your-private-key-id
FIREBASE_PRIVATE_KEY=your-private-key
FIREBASE_CLIENT_EMAIL=your-client-email
FIREBASE_CLIENT_ID=your-client-id
FIREBASE_AUTH_URI=https://accounts.google.com/o/oauth2/auth
FIREBASE_TOKEN_URI=https://oauth2.googleapis.com/token

# API Configuration
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=True
```

## Data Models

### UserProfile
```json
{
  "name": "string",
  "surname": "string",
  "school_name": "string",
  "year": "string",
  "ftcs_status": "string",
  "gpa_range": "string",
  "educational_goals": "string",
  "age": "string",
  "gender": "string",
  "race_ethnicity": "string",
  "working_hours": "string",
  "stress_level": "string",
  "self_efficacy": "string",
  "major": "string",
  "interests": ["string"],
  "email": "string"
}
```

### RecommendationRequest
```json
{
  "user_email": "string",
  "category": "string"
}
```

## CORS Configuration

The API is configured to allow requests from any origin for development. For production, update the CORS settings in `main.py`:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-frontend-domain.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)
```

## Error Handling

The API returns appropriate HTTP status codes:
- `200` - Success
- `400` - Bad Request
- `401` - Unauthorized
- `404` - Not Found
- `500` - Internal Server Error

## Development

### Adding New Endpoints

1. Define Pydantic models for request/response
2. Add the endpoint function with proper error handling
3. Update this README with endpoint documentation

### Testing

```bash
# Install test dependencies
pip install pytest httpx

# Run tests
pytest
```

## Deployment

### Docker (Recommended)

```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Traditional Deployment

1. Set up a production server (Ubuntu/CentOS)
2. Install Python 3.8+ and pip
3. Clone the repository
4. Install dependencies
5. Set up environment variables
6. Use a process manager like PM2 or systemd
7. Set up reverse proxy with Nginx

## Troubleshooting

### Common Issues

1. **Firebase Authentication Error**
   - Check if `firebase-key.json` is in the correct location
   - Verify Firebase project configuration

2. **Data Loading Error**
   - Ensure all CSV/Excel files are present
   - Check file permissions

3. **CORS Issues**
   - Verify CORS configuration
   - Check frontend URL in allowed origins

### Logs

The application logs errors and important events. Check the console output for debugging information.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is proprietary to Campus Connect.
