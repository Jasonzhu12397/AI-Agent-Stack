FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOME=/tmp
WORKDIR /workspace
COPY pyproject.toml README.md ./
COPY apps/platform-api ./apps/platform-api
COPY apps/worker ./apps/worker
RUN pip install .
EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "platform_api.asgi:application"]
USER 10001:10001
