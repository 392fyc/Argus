# Base image: PR-Agent (community-maintained successor of Qodo's codiumai/pr-agent).
# Rebased off the FROZEN codiumai/pr-agent:0.34 (last publish ~2026-05; and per
# qodo-ai/pr-agent#2306 that tag actually shipped 0.32-era code) onto the active
# pragent/pr-agent namespace. Digest-pinned, not a floating tag, per the #2306
# build-pipeline-trust lesson.
#   tag:    pragent/pr-agent:0.38.0-github_app   (PyPI pr-agent 0.38.0, 2026-06-26)
#   source: PyPI author "Qodo AI"; GitHub the-pr-agent/pr-agent (community-owned)
#   digest below is the multi-arch (amd64+arm64) manifest, timestamp-matches PyPI.
# Mercury #498.
FROM pragent/pr-agent:0.38.0-github_app@sha256:f07a88d52c816aef04fee53e31fa99b5ff9fd9dee186e652677f730a2b0888bc

COPY entrypoint-guard.py /app/entrypoint-guard.py
COPY mention_rewrite.py /app/mention_rewrite.py
COPY patch_suggestion_format.py /app/patch_suggestion_format.py
COPY argus_events.py /app/argus_events.py

CMD ["python", "-m", "gunicorn", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-c", "pr_agent/servers/gunicorn_config.py", \
     "--forwarded-allow-ips", "*", \
     "entrypoint-guard:app"]
