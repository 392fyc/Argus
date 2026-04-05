FROM codiumai/pr-agent:0.34-github_app

COPY entrypoint-guard.py /app/entrypoint-guard.py

# Load the guarded app module directly (no --factory needed)
CMD ["python", "-m", "gunicorn", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-c", "pr_agent/servers/gunicorn_config.py", \
     "--forwarded-allow-ips", "*", \
     "entrypoint-guard:app"]
