FROM rust:1.85-bookworm AS build
WORKDIR /src
COPY apps/rust-api/Cargo.toml apps/rust-api/Cargo.lock* ./
RUN mkdir src && printf 'fn main() {}\n' > src/main.rs && cargo build --release --locked || cargo build --release
COPY apps/rust-api/src ./src
COPY apps/rust-api/tests ./tests
RUN touch src/main.rs && cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/ai-agent-stack-api-edge /usr/local/bin/rust-api-edge
ENV RUST_LOG=info RUST_API_LISTEN=0.0.0.0:8080 RUST_API_UPSTREAM=http://api:8000
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/rust-api-edge"]
