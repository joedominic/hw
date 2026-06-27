# ResumeElite — web + Huey worker image (CPU torch + sentence-transformers).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface

WORKDIR /app

COPY requirements.txt requirements-jobspy.txt ./
# libgomp1: PyTorch OpenMP; gcc/g++: compile wheels — removed after pip to shrink the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ libffi-dev libgomp1 \
    && pip install --upgrade pip \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt \
    && pip install --no-deps -r requirements-jobspy.txt \
    && playwright install --with-deps chromium \
    && apt-get purge -y --auto-remove gcc g++ \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY django_project/ django_project/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

WORKDIR /app/django_project

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
