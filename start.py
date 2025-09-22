#!/usr/bin/env python3
"""
Startup script for the Campus Connect FastAPI backend
"""
import uvicorn
import os
import sys
from pathlib import Path

def main():
    # Check if required files exist
    required_files = [
        "firebase-key.json",
        "CC_activities_ex.csv",
        "organizations_with_specific_majors.csv", 
        "filtered_utd_events_with_categories.csv",
        "utd_courses.csv",
        "UTD_tutoring.xlsx"
    ]
    
    missing_files = []
    for file in required_files:
        if not Path(file).exists():
            missing_files.append(file)
    
    if missing_files:
        print("❌ Missing required files:")
        for file in missing_files:
            print(f"   - {file}")
        print("\nPlease ensure all required files are in the CCBack directory.")
        print("See README.md for setup instructions.")
        sys.exit(1)
    
    # Get configuration from environment variables
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    reload = os.getenv("DEBUG", "False").lower() == "true"
    
    print("🚀 Starting Campus Connect API Server...")
    print(f"📍 Host: {host}")
    print(f"🔌 Port: {port}")
    print(f"🔄 Reload: {reload}")
    print(f"📚 API Docs: http://{host}:{port}/docs")
    print("=" * 50)
    
    # Start the server
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info"
    )

if __name__ == "__main__":
    main()
