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
    pub native_identity: bool,
    pub database_url: Option<String>,
    pub django_secret_key: Option<String>,
    pub secure_cookies: bool,
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
        .and_then(|config| {
            config.with_native_identity(
                parse_bool("RUST_API_NATIVE_IDENTITY", false)?,
                env::var("DATABASE_URL").ok(),
                env::var("DJANGO_SECRET_KEY").ok(),
            )
        })
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
            native_identity: false,
            database_url: None,
            django_secret_key: None,
            secure_cookies: false,
        })
    }

    pub fn with_native_identity(
        mut self,
        enabled: bool,
        database_url: Option<String>,
        django_secret_key: Option<String>,
    ) -> Result<Self, String> {
        if enabled && database_url.is_none() {
            return Err("DATABASE_URL is required when RUST_API_NATIVE_IDENTITY is enabled".into());
        }
        if enabled && django_secret_key.is_none() {
            return Err(
                "DJANGO_SECRET_KEY is required when RUST_API_NATIVE_IDENTITY is enabled".into(),
            );
        }
        self.native_identity = enabled;
        self.database_url = database_url;
        self.django_secret_key = django_secret_key;
        let production = env::var("APP_ENV")
            .map(|value| value.eq_ignore_ascii_case("production"))
            .unwrap_or(false);
        self.secure_cookies = parse_bool("RUST_API_SECURE_COOKIES", production)?;
        Ok(self)
    }

    pub fn upstream_health_url(&self) -> String {
        format!(
            "{}/health/ready",
            self.upstream.as_str().trim_end_matches('/')
        )
    }

    /// Map an inbound HTTP path onto the upstream WebSocket endpoint.
    ///
    /// The edge terminates the browser handshake and opens a second handshake
    /// upstream, so the scheme has to be translated rather than reused.
    pub fn upstream_websocket_url(&self, path_and_query: Option<&str>) -> Result<Url, String> {
        let mut url = self.upstream.clone();
        url.set_scheme(if self.upstream.scheme() == "https" {
            "wss"
        } else {
            "ws"
        })
        .map_err(|_| "failed to derive the websocket scheme".to_string())?;
        let (path, query) = match path_and_query {
            Some(value) => value.split_once('?').unwrap_or((value, "")),
            None => ("/", ""),
        };
        url.set_path(if path.is_empty() { "/" } else { path });
        url.set_query(if query.is_empty() { None } else { Some(query) });
        Ok(url)
    }
}

fn parse_bool(name: &str, default: bool) -> Result<bool, String> {
    let raw = env::var(name).unwrap_or_else(|_| default.to_string());
    match raw.to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" => Ok(true),
        "0" | "false" | "no" | "" => Ok(false),
        _ => Err(format!("{name} must be true or false")),
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
