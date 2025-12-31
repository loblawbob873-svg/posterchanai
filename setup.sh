#!/bin/bash

set -e

echo "Setting up Posterchanai..."

# Create upload directory
UPLOAD_PATH="/var/lib/posterchanai"
if [ ! -d "$UPLOAD_PATH" ]; then
    echo "Creating upload directory at $UPLOAD_PATH..."
    sudo mkdir -p "$UPLOAD_PATH"
    sudo chown $(whoami):$(whoami) "$UPLOAD_PATH"
    echo "Upload directory created."
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install requirements
echo "Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "Setup complete!"
echo ""
echo "To run the application:"
echo "  source venv/bin/activate"
echo "  python run.py"
echo ""
echo "Default login: admin / admin"
echo "Access at: http://localhost:8000"
echo "Uploads stored at: $UPLOAD_PATH"
