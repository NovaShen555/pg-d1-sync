FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system pgd1sync \
    && useradd --system --gid pgd1sync --home-dir /app --no-create-home pgd1sync
COPY app /app/app
USER pgd1sync

ENTRYPOINT ["python", "-m", "app.main"]

