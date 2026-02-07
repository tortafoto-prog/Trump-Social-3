FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Install dumb-init to handle zombie processes (PID 1 issue)
RUN apt-get update && apt-get install -y dumb-init && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Make start script executable
RUN chmod +x start.sh

# Use dumb-init as entrypoint to reap zombies, and start.sh as the command loop
ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["./start.sh"]
