FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /workspace
COPY pyproject.toml README.md ./
COPY apps ./apps
RUN pip install .
EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "platform_api.asgi:application"]
