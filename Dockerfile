FROM codiumai/pr-agent:0.34-github_app

# Copy the Argus guard middleware
COPY entrypoint-guard.py /app/entrypoint-guard.py

# Use gunicorn factory mode to load the guarded app
CMD ["python", "-m", "gunicorn", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-c", "pr_agent/servers/gunicorn_config.py", \
     "--forwarded-allow-ips", "*", \
     "--factory", \
     "entrypoint-guard:apply_guard"]
