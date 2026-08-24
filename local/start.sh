#!/bin/bash
# SPHERA Local Server — quick start
# Usage: ./start.sh

echo "Installing dependencies..."
pip install -r requirements.txt --quiet

echo "Starting SPHERA..."
python server.py
