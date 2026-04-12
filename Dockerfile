FROM codiumai/pr-agent:0.34-github_app

COPY entrypoint-guard.py /app/entrypoint-guard.py
COPY patch_suggestion_format.py /app/patch_suggestion_format.py
COPY argus_events.py /app/argus_events.py

CMD ["python", "-m", "gunicorn", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-c", "pr_agent/servers/gunicorn_config.py", \
     "--forwarded-allow-ips", "*", \
     "entrypoint-guard:app"]
