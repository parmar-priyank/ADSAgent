# Gunicorn configuration for production deployment on Ubuntu 24
# Used by: gunicorn -c gunicorn.conf.py main:app

import multiprocessing

# Bind to Unix socket — Nginx talks to this, never exposed to internet
bind = "unix:/run/adsagent/gunicorn.sock"

# Worker class — uvicorn workers handle async FastAPI correctly
worker_class = "uvicorn.workers.UvicornWorker"

# 2 workers per CPU core is the standard starting point for I/O-bound apps.
# Adjust down if the server has very little RAM (each worker ~150-200 MB).
workers = multiprocessing.cpu_count() * 2

# Restart a worker after this many requests to prevent memory leaks
max_requests = 1000
max_requests_jitter = 100

# Kill a worker that takes longer than 300 s on one request (large ZIP/PDF uploads + AI processing)
timeout = 300

# Keep idle worker-to-Nginx connections alive for 5 s
keepalive = 5

# Log to stdout/stderr — systemd captures these via journald
accesslog = "-"
errorlog  = "-"
loglevel  = "info"

# Run as this user/group (created during deployment)
user  = "adsagent"
group = "adsagent"

# Ensure the socket directory exists and has correct permissions
import os
os.makedirs("/run/adsagent", exist_ok=True)
