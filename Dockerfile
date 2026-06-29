FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
ADD https://astral.sh/uv/install.sh /install.sh
RUN sh /install.sh && rm /install.sh
ENV PATH="/root/.local/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
RUN uv run python manage.py collectstatic --noinput
CMD ["uv", "run", "gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "120"]
