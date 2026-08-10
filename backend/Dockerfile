# 3.13 to match the dev venv and ruff's target-version — the container ran
# a different interpreter than every test that ever gated a commit.
FROM python:3.13-slim

WORKDIR /app

# Exact pins from the venv the tests run on; requirements.txt stays the
# human-readable intent.
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
