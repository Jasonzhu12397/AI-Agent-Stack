use std::env;

/// Runtime configuration for the Rust platform API.
#[derive(Clone, Debug)]
pub struct Config {
    pub listen: String,
    pub database_url: Option<String>,
    pub secure_cookies: bool,
    pub external_scheme: String,
    pub media_root: String,
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        let listen = env::var("RUST_API_LISTEN").unwrap_or_else(|_| "0.0.0.0:8080".into());
        let database_url = env::var("DATABASE_URL")
            .map_err(|_| "DATABASE_URL is required by the Rust platform API".to_string())?;
        if database_url.trim().is_empty() {
            return Err("DATABASE_URL must not be empty".into());
        }

        let production = env::var("APP_ENV")
            .map(|value| value.eq_ignore_ascii_case("production"))
            .unwrap_or(false);
        let secure_cookies = parse_bool("RUST_API_SECURE_COOKIES", production)?;
        let default_scheme = if production { "https" } else { "http" };
        let external_scheme =
            env::var("RUST_API_EXTERNAL_SCHEME").unwrap_or_else(|_| default_scheme.into());
        if !matches!(external_scheme.as_str(), "http" | "https") {
            return Err("RUST_API_EXTERNAL_SCHEME must be http or https".into());
        }

        Ok(Self {
            listen,
            database_url: Some(database_url),
            secure_cookies,
            external_scheme,
            media_root: env::var("MEDIA_ROOT").unwrap_or_else(|_| ".devdata/media".into()),
        })
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

#[cfg(test)]
mod tests {
    #[test]
    fn rejects_invalid_external_scheme() {
        assert!(!matches!("ftp", "http" | "https"));
    }
}
