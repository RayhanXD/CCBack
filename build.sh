#!/bin/bash
# Build script for Railway deployment

echo "Starting build process..."

# Check Python version
echo "Python version:"
python --version

# Check pip version
echo "Pip version:"
pip --version

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Verify uvicorn is installed
echo "Verifying uvicorn installation..."
python -c "import uvicorn; print('uvicorn version:', uvicorn.__version__)"

# Verify FastAPI is installed
echo "Verifying FastAPI installation..."
python -c "import fastapi; print('FastAPI version:', fastapi.__version__)"

echo "Build completed successfully!"
