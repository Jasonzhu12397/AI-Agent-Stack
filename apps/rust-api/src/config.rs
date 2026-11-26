use std::env;
use std::time::Duration;

use reqwest::Url;

/// Runtime configuration for the edge API.
#[derive(Clone, Debug)]
pub struct Config {
    pub listen: String,
    pub upstream: Url,
    pub upstream_timeout: Duration,
    pub request_timeout: Duration,
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        let listen = env::var("RUST_API_LISTEN").unwrap_or_else(|_| "0.0.0.0:8080".into());
        let upstream =
            env::var("RUST_API_UPSTREAM").unwrap_or_else(|_| "http://127.0.0.1:8000".into());
        Self::new(
            listen,
            upstream,
            parse_seconds("RUST_API_UPSTREAM_TIMEOUT_SECONDS", 3)?,
            parse_seconds("RUST_API_REQUEST_TIMEOUT_SECONDS", 60)?,
        )
    }

    pub fn new(
        listen: String,
        upstream: String,
        upstream_timeout: Duration,
        request_timeout: Duration,
    ) -> Result<Self, String> {
        let upstream = Url::parse(&upstream)
            .map_err(|error| format!("RUST_API_UPSTREAM is invalid: {error}"))?;
        if !matches!(upstream.scheme(), "http" | "https") {
            return Err("RUST_API_UPSTREAM must use http or https".into());
        }
        if upstream.path() != "/" && !upstream.path().is_empty() {
            return Err(
                "RUST_API_UPSTREAM must not contain a path; routes are forwarded as-is".into(),
            );
        }
        if upstream_timeout.is_zero() || request_timeout.is_zero() {
            return Err("timeouts must be greater than zero".into());
        }
        if request_timeout < upstream_timeout {
            return Err("RUST_API_REQUEST_TIMEOUT_SECONDS must be >= the upstream timeout".into());
        }
        Ok(Self {
            listen,
            upstream,
            upstream_timeout,
            request_timeout,
        })
    }

    pub fn upstream_health_url(&self) -> String {
        format!(
            "{}/health/ready",
            self.upstream.as_str().trim_end_matches('/')
        )
    }
}

fn parse_seconds(name: &str, default: u64) -> Result<Duration, String> {
    let raw = env::var(name).unwrap_or_else(|_| default.to_string());
    let seconds = raw
        .parse::<u64>()
        .map_err(|_| format!("{name} must be a positive number of seconds"))?;
    if seconds == 0 {
        return Err(format!("{name} must be greater than zero"));
    }
    Ok(Duration::from_secs(seconds))
}
