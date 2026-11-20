"""Deployment artefact generation and pre-flight/rollback guidance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scanner import ProjectIndex


def generate_dockerfile(index: ProjectIndex) -> str:
    languages = index.technology.get("languages", [])
    if "python" in languages:
        return """FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml requirements.txt* ./
RUN pip install --no-cache-dir .
COPY . .
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "app"]
"""
    if "javascript" in languages or "typescript" in languages:
        return """FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build --if-present

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY package*.json ./
RUN npm ci --omit=dev
COPY --from=build /app/dist ./dist
USER 1000:1000
EXPOSE 3000
CMD ["node", "dist/index.js"]
"""
    if "go" in languages:
        return """FROM golang:1.22 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/app ./...

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/app /app
USER nonroot:nonroot
ENTRYPOINT ["/app"]
"""
    raise ValueError("no supported language detected for a Dockerfile")


def generate_kubernetes_manifests(name: str, image: str, port: int = 8000) -> dict[str, str]:
    deployment = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
spec:
  replicas: 2
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
        - name: {name}
          image: {image}
          ports:
            - containerPort: {port}
          readinessProbe:
            httpGet:
              path: /healthz
              port: {port}
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /healthz
              port: {port}
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 1Gi
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
"""
    service = f"""apiVersion: v1
kind: Service
metadata:
  name: {name}
spec:
  selector:
    app: {name}
  ports:
    - port: 80
      targetPort: {port}
"""
    return {"deployment.yaml": deployment, "service.yaml": service}


def generate_ci(technology: dict) -> str:
    languages = technology.get("languages", [])
    steps = ["      - uses: actions/checkout@v4"]
    if "python" in languages:
        steps += [
            "      - uses: actions/setup-python@v5\n"
            "        with:\n"
            "          python-version: '3.12'",
            "      - run: pip install -e '.[dev]'\n"
            "      - run: ruff check .\n"
            "      - run: python -m pytest -q",
        ]
    if "javascript" in languages or "typescript" in languages:
        steps += [
            "      - uses: actions/setup-node@v4\n        with:\n          node-version: '20'",
            "      - run: npm ci\n"
            "      - run: npm test --if-present\n"
            "      - run: npm run build --if-present",
        ]
    if "go" in languages:
        steps += [
            "      - uses: actions/setup-go@v5\n        with:\n          go-version: '1.22'",
            "      - run: go test ./...",
        ]
    header = (
        "name: CI\non: [push, pull_request]\n"
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
    )
    return header + "\n".join(steps) + "\n"


@dataclass
class PreflightResult:
    ready: bool
    reasons: list[str]
    capacity: dict[str, Any]


def preflight(
    nodes: list[dict],
    required_cpu_millicores: int = 100,
    required_memory_mib: int = 256,
) -> PreflightResult:
    reasons: list[str] = []
    if not nodes:
        return PreflightResult(False, ["cluster has no nodes"], {})
    ready_nodes = [node for node in nodes if node.get("ready") is True]
    if not ready_nodes:
        reasons.append("no node reports Ready")
    schedulable = [
        node
        for node in ready_nodes
        if not node.get("unschedulable", False) and not node.get("taints")
    ]
    if not schedulable:
        reasons.append("no schedulable node without taints")
    cpu_total = sum(int(node.get("allocatable_cpu_millicores", 0)) for node in schedulable)
    memory_total = sum(int(node.get("allocatable_memory_mib", 0)) for node in schedulable)
    if cpu_total < required_cpu_millicores:
        reasons.append(f"insufficient CPU: {cpu_total}m < {required_cpu_millicores}m")
    if memory_total < required_memory_mib:
        reasons.append(f"insufficient memory: {memory_total}Mi < {required_memory_mib}Mi")
    return PreflightResult(
        ready=not reasons,
        reasons=reasons,
        capacity={
            "schedulable_nodes": len(schedulable),
            "cpu_millicores": cpu_total,
            "memory_mib": memory_total,
        },
    )


def rollback_steps(deployment: str, namespace: str = "default") -> list[str]:
    """Return a human-reviewed rollback plan; the agent never executes it."""

    return [
        f"kubectl -n {namespace} rollout status deployment/{deployment}",
        f"kubectl -n {namespace} rollout history deployment/{deployment}",
        f"kubectl -n {namespace} rollout undo deployment/{deployment}",
        f"kubectl -n {namespace} rollout status deployment/{deployment}",
        "confirm readiness probes and error rate before closing the incident",
    ]


def write_manifests(
    index: ProjectIndex, output: Path, name: str, image: str, port: int = 8000
) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    written = []
    dockerfile = output / "Dockerfile.generated"
    dockerfile.write_text(generate_dockerfile(index), encoding="utf-8")
    written.append(dockerfile)
    for filename, content in generate_kubernetes_manifests(name, image, port).items():
        path = output / filename
        path.write_text(content, encoding="utf-8")
        written.append(path)
    ci = output / "ci.generated.yml"
    ci.write_text(generate_ci(index.technology), encoding="utf-8")
    written.append(ci)
    return written
