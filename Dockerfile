FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system sync \
    && useradd --system --gid sync --home-dir /app --no-create-home sync
COPY app /app/app
USER pgd1sync

ENTRYPOINT ["python", "-m", "app.main"]

