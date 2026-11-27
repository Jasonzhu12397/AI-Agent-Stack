use sha2::{Digest, Sha256};

const SESSION_COOKIE: &str = "sessionid";
const SESSION_AGE_SECONDS: i64 = 1_209_600;

pub fn new_session_key() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

pub fn session_token_hash(token: &str) -> String {
    hex::encode(Sha256::digest(token.as_bytes()))
}

pub fn session_age_seconds() -> i64 {
    SESSION_AGE_SECONDS
}

pub fn session_key_from_headers(headers: &axum::http::HeaderMap) -> Option<String> {
    if let Some(token) = headers
        .get("x-session-token")
        .and_then(|value| value.to_str().ok())
        .map(str::trim)
        .filter(|token| !token.is_empty() && token.len() <= 128)
    {
        return Some(token.to_string());
    }
    headers
        .get("cookie")
        .and_then(|value| value.to_str().ok())
        .and_then(|cookie| {
            cookie.split(';').find_map(|pair| {
                let (name, value) = pair.trim().split_once('=')?;
                (name == SESSION_COOKIE).then(|| value.trim().to_string())
            })
        })
        .filter(|key| !key.is_empty() && key.len() <= 128)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_tokens_are_stored_as_one_way_hashes() {
        let token = new_session_key();
        let hash = session_token_hash(&token);
        assert_ne!(hash, token);
        assert_eq!(hash, session_token_hash(&token));
        assert_eq!(hash.len(), 64);
    }

    #[test]
    fn extracts_the_cookie_after_other_cookies() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert(
            "cookie",
            "csrftoken=abc; sessionid=session-key; theme=dark"
                .parse()
                .unwrap(),
        );
        assert_eq!(
            session_key_from_headers(&headers).as_deref(),
            Some("session-key")
        );
    }

    #[test]
    fn prefers_the_session_token_header() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert("cookie", "sessionid=cookie-key".parse().unwrap());
        headers.insert("x-session-token", "header-key".parse().unwrap());
        assert_eq!(
            session_key_from_headers(&headers).as_deref(),
            Some("header-key")
        );
    }
}
