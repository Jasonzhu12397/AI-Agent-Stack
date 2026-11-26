use std::collections::BTreeMap;

use base64::Engine;
use flate2::read::ZlibDecoder;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

const SESSION_COOKIE: &str = "sessionid";
const SESSION_AGE_SECONDS: i64 = 1209600;
const SESSION_SALT: &str = "django.contrib.sessions.SessionStore";

type HmacSha256 = Hmac<Sha256>;

/// Decode the value stored in `django_session.session_data`.
///
/// Django signs the payload with `TimestampSigner` (salt + "signer"), base64url
/// encodes it without padding, optionally zlib-compresses it behind a leading
/// `.`, and stores compact JSON. The Rust service has to accept exactly the
/// same format so a session minted by either side works for both.
pub fn decode_session_data(value: &str, secret_key: &str) -> Result<serde_json::Value, String> {
    let mut parts = value.rsplitn(3, ':');
    let signature = parts.next().ok_or("session signature is missing")?;
    let timestamp = parts.next().ok_or("session timestamp is missing")?;
    let payload = parts.next().ok_or("session payload is missing")?;
    let signed = format!("{payload}:{timestamp}");

    let mut mac = HmacSha256::new_from_slice(&signing_key(secret_key))
        .map_err(|_| "failed to initialise the session signature".to_string())?;
    mac.update(signed.as_bytes());
    let expected =
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
    if !constant_time_eq(signature.as_bytes(), expected.as_bytes()) {
        return Err("session signature does not match".into());
    }

    let compressed = payload.starts_with('.');
    let encoded = payload.strip_prefix('.').unwrap_or(payload);
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(encoded)
        .map_err(|error| format!("session payload is not base64url: {error}"))?;
    let bytes = if compressed {
        let mut decoder = ZlibDecoder::new(bytes.as_slice());
        let mut decoded = Vec::new();
        std::io::Read::read_to_end(&mut decoder, &mut decoded)
            .map_err(|error| format!("session payload is not valid zlib: {error}"))?;
        decoded
    } else {
        bytes
    };
    serde_json::from_slice(&bytes).map_err(|error| format!("session payload is not JSON: {error}"))
}

/// Build the signed value Django's `SessionStore` writes to
/// `django_session.session_data`, using the same JSON serializer, zlib
/// compression rule and `TimestampSigner` layout as Python.
pub fn encode_session_data(
    user_id: i64,
    password: &str,
    secret_key: &str,
) -> Result<String, String> {
    let auth_hash = salted_hmac_hex(
        "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash",
        password,
        secret_key,
    );
    let mut session: BTreeMap<&str, String> = BTreeMap::new();
    session.insert("_auth_user_id", user_id.to_string());
    session.insert(
        "_auth_user_backend",
        "django.contrib.auth.backends.ModelBackend".to_string(),
    );
    session.insert("_auth_user_hash", auth_hash);
    let json = serde_json::to_vec(&session).map_err(|error| error.to_string())?;

    let mut compressed = Vec::new();
    {
        use std::io::Write;
        let mut encoder =
            flate2::write::ZlibEncoder::new(&mut compressed, flate2::Compression::default());
        encoder
            .write_all(&json)
            .map_err(|error| error.to_string())?;
        encoder.finish().map_err(|error| error.to_string())?;
    }
    let payload = if compressed.len() < json.len().saturating_sub(1) {
        format!(
            ".{}",
            base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&compressed)
        )
    } else {
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&json)
    };
    let timestamp = base62_encode(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|error| error.to_string())?
            .as_secs(),
    );
    let signed = format!("{payload}:{timestamp}");
    let mut mac = HmacSha256::new_from_slice(&signing_key(secret_key))
        .map_err(|_| "failed to initialise the session signature".to_string())?;
    mac.update(signed.as_bytes());
    let signature =
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
    Ok(format!("{signed}:{signature}"))
}

pub fn salted_hmac_hex(key_salt: &str, value: &str, secret_key: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!("{key_salt}{secret_key}").as_bytes());
    let key = hasher.finalize();
    let mut mac = HmacSha256::new_from_slice(&key).expect("HMAC accepts any key length");
    mac.update(value.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

pub fn new_session_key() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

pub fn session_age_seconds() -> i64 {
    SESSION_AGE_SECONDS
}

fn base62_encode(mut value: u64) -> String {
    const ALPHABET: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    if value == 0 {
        return "0".into();
    }
    let mut encoded = Vec::new();
    while value > 0 {
        encoded.push(ALPHABET[(value % 62) as usize]);
        value /= 62;
    }
    encoded.reverse();
    String::from_utf8(encoded).expect("base62 alphabet is ASCII")
}

fn signing_key(secret_key: &str) -> Vec<u8> {
    let mut hasher = Sha256::new();
    hasher.update(format!("{SESSION_SALT}signer{secret_key}").as_bytes());
    hasher.finalize().to_vec()
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0u8, |acc, (a, b)| acc | (a ^ b))
        == 0
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
    fn rejects_a_payload_signed_with_another_key() {
        let encoded = ".eJyrVopPLC3JiC8tTi2Kz0xRslIyVNJBFktKTM5OzQNJpGQl5qXn6yXn55UUZSbpgZToQWWL9XzzU1JznKBqawE67h8t:1xWUBh:NCsVY_bZ8nngCam2cZnbhdgYpsgRgDGpkLE7FARYBiY";
        assert!(decode_session_data(encoded, "not-the-key").is_err());
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
