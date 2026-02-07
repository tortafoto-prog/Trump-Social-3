#!/bin/bash
set -e

# Infinite loop to keep the scraper running
while true; do
    echo "🚀 Starting Trump Scraper..."
    python main.py
    
    echo "⚠️ main.py exited (Exit code: $?). Restarting in 5 seconds..."
    sleep 5
done
