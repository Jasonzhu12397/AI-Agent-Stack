FROM rust:1.92-bookworm AS build
WORKDIR /src
COPY apps/rust-api/Cargo.toml apps/rust-api/Cargo.lock ./
RUN mkdir src && printf 'fn main() {}\n' > src/main.rs && cargo build --release --locked
COPY apps/rust-api/src ./src
COPY apps/rust-api/tests ./tests
RUN touch src/main.rs && cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/ai-agent-stack-api /usr/local/bin/rust-api
ENV RUST_LOG=info RUST_API_LISTEN=0.0.0.0:8080
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=10 CMD curl --fail --silent http://127.0.0.1:8080/health/ready || exit 1
ENTRYPOINT ["/usr/local/bin/rust-api"]
