#!/usr/bin/env python3
"""
Startup script for Railway deployment
This ensures we use the system Python and avoid virtual environment issues
"""
import os
import sys
import subprocess

def main():
    # Get the port from environment variable
    port = os.getenv("PORT", "8000")
    
    # Start uvicorn with the system Python
    cmd = [
        sys.executable,  # Use the current Python interpreter
        "-m", "uvicorn",
        "main:app",
        "--host", "0.0.0.0",
        "--port", port
    ]
    
    print(f"Starting server with command: {' '.join(cmd)}")
    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version}")
    
    # Start the server
    subprocess.run(cmd)

if __name__ == "__main__":
    main()