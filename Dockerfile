FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOME=/tmp PYTHONPATH=/workspace/apps/platform-api:/workspace/apps/worker:/workspace/apps:/workspace
WORKDIR /workspace
COPY pyproject.toml README.md ./
COPY apps/platform-api ./apps/platform-api
COPY apps/worker ./apps/worker
COPY apps/__init__.py ./apps/__init__.py
# The whole shared package tree ships, not a hand-listed subset: a new module
# under packages/ must never break an image that forgot to copy it.
COPY packages ./packages
COPY apps/eph_agent ./apps/eph_agent
COPY tools ./tools
RUN pip install .
EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "platform_api.asgi:application"]
USER 10001:10001
