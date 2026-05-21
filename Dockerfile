# ResumeElite — web + Huey worker image (torch + sentence-transformers).
FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface

WORKDIR /app

# Build tools for wheels (cryptography, etc.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install gunicorn

COPY django_project/ django_project/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

WORKDIR /app/django_project

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
