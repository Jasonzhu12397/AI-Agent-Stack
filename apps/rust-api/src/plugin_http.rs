//! Manifest-driven HTTP connector operations.
//!
//! A Studio plugin publishes `manifest.http.operations[capability]` with a
//! method, a path, and the argument mappings for path, query and body. The
//! invocation must honour those mappings: posting the raw argument object at
//! the plugin endpoint reaches no real operation. This module renders the
//! request exactly as the platform connector did and maps upstream statuses
//! onto the stable capability fault codes.

use serde_json::{json, Map, Value};

use crate::proxy::AppState;

#[derive(Debug)]
pub struct HttpFault {
    pub code: &'static str,
    pub message: String,
}

impl HttpFault {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

const ALLOWED_METHODS: [&str; 5] = ["GET", "POST", "PUT", "PATCH", "DELETE"];

fn forbidden_header(name: &str) -> bool {
    matches!(
        name.to_ascii_lowercase().as_str(),
        "authorization"
            | "proxy-authorization"
            | "cookie"
            | "set-cookie"
            | "host"
            | "content-length"
            | "transfer-encoding"
            | "connection"
            | "upgrade"
            | "te"
            | "trailer"
            | "expect"
            | "x-goog-api-key"
            | "x-api-key"
    )
}

fn valid_header_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || "-_.!#$%&'*+^`|~".contains(ch))
}

fn encode_component(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                encoded.push(*byte as char)
            }
            other => encoded.push_str(&format!("%{other:02X}")),
        }
    }
    encoded
}

fn mapped(operation: &Value, key: &str) -> Map<String, Value> {
    operation
        .get(key)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn argument_text(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        _ => None,
    }
}

/// Resolve a credential reference into request headers and an optional query
/// credential. OAuth contracts refresh before use so a connector never sends
/// an expired access token.
async fn resolve_auth(
    state: &AppState,
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    credential_ref: &str,
) -> Result<
    (
        Vec<(String, String)>,
        Option<(String, String)>,
        Option<String>,
    ),
    HttpFault,
> {
    if credential_ref.trim().is_empty() {
        return Err(HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Connector credential reference is missing.",
        ));
    }
    let Some(secret) = state.config.platform_secret_key.as_deref() else {
        return Err(HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Platform secret key is not configured.",
        ));
    };
    let Some(id) = credential_ref.strip_prefix("plugin://") else {
        return Err(HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Connector credential reference is invalid.",
        ));
    };
    let sealed: Option<String> = sqlx::query_scalar(
        "SELECT sealed_value FROM core_agentplugincredential \
         WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ? LIMIT 1",
    )
    .bind(id)
    .bind(tenant_id)
    .fetch_optional(pool)
    .await
    .map_err(|_| {
        HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Connector credential could not be read.",
        )
    })?;
    let Some(sealed) = sealed else {
        return Err(HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Connector credential could not be resolved.",
        ));
    };
    let Some(plain) =
        crate::plugin_studio_api::open_scope(secret, "agent-plugin-credential", &sealed)
    else {
        return Err(HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Connector credential could not be decrypted.",
        ));
    };
    let Ok(credential) = serde_json::from_str::<Value>(&plain) else {
        return Err(HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Connector credential has an invalid shape.",
        ));
    };
    let credential_type = credential.get("type").and_then(Value::as_str).unwrap_or("");
    match credential_type {
        "bearer" => {
            let token = credential
                .get("token")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty() && !value.contains('\n'))
                .ok_or_else(|| {
                    HttpFault::new(
                        "CAPABILITY_AUTH_REQUIRED",
                        "Connector bearer token is invalid.",
                    )
                })?;
            Ok((
                vec![("Authorization".into(), format!("Bearer {token}"))],
                None,
                None,
            ))
        }
        "header" => {
            let name = credential.get("name").and_then(Value::as_str).unwrap_or("");
            let value = credential
                .get("value")
                .and_then(Value::as_str)
                .unwrap_or("");
            if !valid_header_name(name) || value.is_empty() || value.contains('\n') {
                return Err(HttpFault::new(
                    "CAPABILITY_AUTH_REQUIRED",
                    "Connector header credential is invalid.",
                ));
            }
            Ok((vec![(name.to_string(), value.to_string())], None, None))
        }
        "query" => {
            let name = credential.get("name").and_then(Value::as_str).unwrap_or("");
            let value = credential
                .get("value")
                .and_then(Value::as_str)
                .unwrap_or("");
            if name.is_empty() || value.is_empty() {
                return Err(HttpFault::new(
                    "CAPABILITY_AUTH_REQUIRED",
                    "Connector query credential is invalid.",
                ));
            }
            Ok((
                Vec::new(),
                Some((name.to_string(), value.to_string())),
                None,
            ))
        }
        "oauth2" => {
            let token =
                refresh_oauth(state, pool, tenant_id, id, secret, credential.clone()).await?;
            Ok((
                vec![("Authorization".into(), format!("Bearer {token}"))],
                None,
                None,
            ))
        }
        _ => Err(HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Connector credential type is unsupported.",
        )),
    }
}

/// Refresh an OAuth contract when its access token is missing or about to
/// expire, and persist the renewed contract so the next call reuses it.
async fn refresh_oauth(
    _state: &AppState,
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    credential_id: &str,
    secret: &str,
    contract: Value,
) -> Result<String, HttpFault> {
    let access_token = contract
        .get("access_token")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let expires_at = contract
        .get("access_token_expires_at")
        .and_then(Value::as_str)
        .and_then(|value| chrono::DateTime::parse_from_rfc3339(value).ok());
    let fresh = !access_token.is_empty()
        && expires_at
            .map(|stamp| stamp > chrono::Utc::now() + chrono::Duration::seconds(60))
            .unwrap_or(false);
    if fresh {
        return Ok(access_token);
    }
    let token_url = contract
        .get("token_url")
        .and_then(Value::as_str)
        .unwrap_or("");
    let client_id = contract
        .get("client_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let client_secret = contract
        .get("client_secret")
        .and_then(Value::as_str)
        .unwrap_or("");
    let refresh_token = contract
        .get("refresh_token")
        .and_then(Value::as_str)
        .unwrap_or("");
    if token_url.is_empty() || client_id.is_empty() || refresh_token.is_empty() {
        return Err(HttpFault::new(
            "CAPABILITY_AUTH_REQUIRED",
            "Connector OAuth contract is incomplete.",
        ));
    }
    let mut form = vec![
        ("grant_type".to_string(), "refresh_token".to_string()),
        ("client_id".to_string(), client_id.to_string()),
        ("refresh_token".to_string(), refresh_token.to_string()),
    ];
    if !client_secret.is_empty() {
        form.push(("client_secret".to_string(), client_secret.to_string()));
    }
    if let Some(scope) = contract.get("scope").and_then(Value::as_str) {
        if !scope.is_empty() {
            form.push(("scope".to_string(), scope.to_string()));
        }
    }
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(20))
        .build()
        .map_err(|error| HttpFault::new("CAPABILITY_UPSTREAM_ERROR", error.to_string()))?;
    let response = client
        .post(token_url)
        .form(&form)
        .send()
        .await
        .map_err(|error| {
            HttpFault::new(
                "CAPABILITY_UPSTREAM_UNREACHABLE",
                format!("The token endpoint could not be reached: {error}"),
            )
        })?;
    let status = response.status();
    let body: Value = response.json().await.unwrap_or_else(|_| json!({}));
    if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_AUTH_REJECTED",
            "The token endpoint rejected the refresh token.",
        ));
    }
    if !status.is_success() {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_ERROR",
            "The token endpoint refused to refresh the credential.",
        ));
    }
    let new_token = body
        .get("access_token")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            HttpFault::new("CAPABILITY_UPSTREAM_ERROR", "No access token was issued.")
        })?;
    let expires_in = body
        .get("expires_in")
        .and_then(Value::as_i64)
        .unwrap_or(3600);
    let mut updated = contract.clone();
    if let Some(object) = updated.as_object_mut() {
        object.insert("access_token".into(), json!(new_token));
        object.insert(
            "access_token_expires_at".into(),
            json!((chrono::Utc::now() + chrono::Duration::seconds(expires_in)).to_rfc3339()),
        );
    }
    if let Ok(contract_text) = serde_json::to_string(&updated) {
        let _ = sealed_update(pool, tenant_id, credential_id, secret, &contract_text).await;
    }
    Ok(new_token.to_string())
}

async fn sealed_update(
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    credential_id: &str,
    secret: &str,
    plaintext: &str,
) -> Result<(), ()> {
    let sealed = crate::plugin_studio_api::seal_scope(secret, "agent-plugin-credential", plaintext)
        .map_err(|_| ())?;
    let now = "datetime('now')";
    sqlx::query(&format!(
        "UPDATE core_agentplugincredential SET sealed_value = ?, created_at = {now} \
         WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?"
    ))
    .bind(&sealed)
    .bind(credential_id)
    .bind(tenant_id)
    .execute(pool)
    .await
    .map(|_| ())
    .map_err(|_| ())
}

/// How a connector authenticates: the tenant that owns the credential, the
/// installation's reference and whether the plugin requires one at all.
pub struct ConnectorAuth<'a> {
    pub state: &'a AppState,
    pub pool: &'a sqlx::AnyPool,
    pub tenant_id: &'a str,
    pub credential_ref: &'a str,
    pub auth_required: bool,
}

impl ConnectorAuth<'_> {
    async fn resolve(
        &self,
    ) -> Result<
        (
            Vec<(String, String)>,
            Option<(String, String)>,
            Option<String>,
        ),
        HttpFault,
    > {
        resolve_auth(self.state, self.pool, self.tenant_id, self.credential_ref).await
    }
}

/// Execute one operation from the plugin manifest.
pub async fn invoke_operation(
    auth: ConnectorAuth<'_>,
    manifest: &Value,
    capability: &str,
    arguments: &Value,
    timeout_seconds: i64,
) -> Result<Value, HttpFault> {
    let http = manifest
        .get("http")
        .ok_or_else(|| HttpFault::new("CAPABILITY_INVOCATION_FAILED", "Nothing to invoke."))?;
    let operation = http
        .get("operations")
        .and_then(|operations| operations.get(capability))
        .ok_or_else(|| {
            HttpFault::new(
                "CAPABILITY_INVOCATION_FAILED",
                "The plugin declares no operation for this capability.",
            )
        })?;
    let base_url = http
        .get("base_url")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let method = operation
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_uppercase();
    if !ALLOWED_METHODS.contains(&method.as_str()) || base_url.is_empty() {
        return Err(HttpFault::new(
            "CAPABILITY_INVOCATION_FAILED",
            "Connector HTTP method is not allowed.",
        ));
    }
    let path = operation.get("path").and_then(Value::as_str).unwrap_or("");
    if !path.starts_with('/')
        || path.starts_with("//")
        || path.contains('?')
        || path.contains('#')
        || path.contains("..")
    {
        return Err(HttpFault::new(
            "CAPABILITY_INVOCATION_FAILED",
            "Connector HTTP path is invalid.",
        ));
    }
    let body_param = operation.get("body_param").and_then(Value::as_str);
    let supplied = arguments.as_object().cloned().unwrap_or_default();
    let mut rendered = path.to_string();
    for (placeholder, argument) in mapped(operation, "path_params") {
        let name = argument.as_str().unwrap_or_default().to_string();
        let value = supplied.get(&name).and_then(argument_text).ok_or_else(|| {
            HttpFault::new(
                "CAPABILITY_INVOCATION_FAILED",
                "Connector HTTP argument mapping does not match its input schema.",
            )
        })?;
        rendered = rendered.replace(&format!("{{{placeholder}}}"), &encode_component(&value));
    }
    if rendered.contains('{') || rendered.contains('}') {
        return Err(HttpFault::new(
            "CAPABILITY_INVOCATION_FAILED",
            "Connector HTTP path contains an unresolved parameter.",
        ));
    }
    let mut query: Vec<(String, String)> = Vec::new();
    for (key, argument) in mapped(operation, "query_params") {
        let name = argument.as_str().unwrap_or_default().to_string();
        let Some(value) = supplied.get(&name) else {
            continue;
        };
        let values: Vec<&Value> = match value {
            Value::Array(items) => items.iter().collect(),
            other => vec![other],
        };
        for item in values {
            match item {
                Value::Null | Value::Bool(_) | Value::Object(_) | Value::Array(_) => {
                    return Err(HttpFault::new(
                        "CAPABILITY_INVOCATION_FAILED",
                        "Connector query parameters must be scalar values.",
                    ))
                }
                other => query.push((
                    key.clone(),
                    other
                        .as_str()
                        .map(str::to_string)
                        .unwrap_or_else(|| other.to_string()),
                )),
            }
        }
    }
    let mut headers: Vec<(String, String)> = vec![("Accept".into(), "application/json".into())];
    for (name, value) in mapped(operation, "headers") {
        let Some(text) = value
            .as_str()
            .filter(|text| !text.is_empty() && !text.contains('\n'))
        else {
            return Err(HttpFault::new(
                "CAPABILITY_INVOCATION_FAILED",
                "Connector operation contains an unsafe static header.",
            ));
        };
        if forbidden_header(&name) || !valid_header_name(&name) {
            return Err(HttpFault::new(
                "CAPABILITY_INVOCATION_FAILED",
                "Connector operation contains an unsafe static header.",
            ));
        }
        headers.push((name, text.to_string()));
    }
    if auth.auth_required {
        let (auth_headers, query_credential, _) = auth.resolve().await?;
        let auth_required_name = operation
            .get("auth_query_parameter")
            .and_then(Value::as_str)
            .map(str::to_string);
        match query_credential {
            Some((name, value)) => {
                if auth_required_name.as_deref() != Some(name.as_str()) {
                    return Err(HttpFault::new(
                        "CAPABILITY_INVOCATION_FAILED",
                        "Connector query credential does not match the operation configuration.",
                    ));
                }
                query.push((name, value));
            }
            None => {
                if auth_required_name.is_some() {
                    return Err(HttpFault::new(
                        "CAPABILITY_INVOCATION_FAILED",
                        "Connector operation requires a query credential.",
                    ));
                }
                headers.extend(auth_headers);
            }
        }
    }
    let body = body_param.and_then(|name| supplied.get(name)).cloned();
    if method == "GET" && body.is_some() {
        return Err(HttpFault::new(
            "CAPABILITY_INVOCATION_FAILED",
            "GET connector operations cannot send a request body.",
        ));
    }
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(
            timeout_seconds.clamp(1, 300) as u64,
        ))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|error| HttpFault::new("CAPABILITY_UPSTREAM_ERROR", error.to_string()))?;
    let url = format!("{}{rendered}", base_url.trim_end_matches('/'));
    let mut request = client.request(
        reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| {
            HttpFault::new(
                "CAPABILITY_INVOCATION_FAILED",
                "Connector HTTP method is not allowed.",
            )
        })?,
        &url,
    );
    for (name, value) in &headers {
        request = request.header(name, value);
    }
    if !query.is_empty() {
        request = request.query(&query);
    }
    if let Some(body) = body {
        request = request.json(&body);
    }
    let started = std::time::Instant::now();
    let response = request.send().await.map_err(|error| {
        if error.is_timeout() {
            HttpFault::new("CAPABILITY_TIMEOUT", "Connector HTTP request timed out.")
        } else {
            HttpFault::new(
                "CAPABILITY_UPSTREAM_UNREACHABLE",
                "Connector endpoint is unreachable.",
            )
        }
    })?;
    let status = response.status();
    let _ = started;
    if status.is_redirection() {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_UNREACHABLE",
            "Connector redirects are not allowed.",
        ));
    }
    if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_AUTH_REJECTED",
            "Connector rejected the configured credential.",
        ));
    }
    if status.is_client_error() {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_CLIENT_ERROR",
            "Connector returned a 4xx status.",
        ));
    }
    if status.is_server_error() {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_SERVER_ERROR",
            "Connector returned a 5xx status.",
        ));
    }
    let content_type = response
        .headers()
        .get("content-type")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if content_type != "application/json" && !content_type.ends_with("+json") {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_INVALID_RESPONSE",
            "Connector response content type must be JSON.",
        ));
    }
    let bytes = response.bytes().await.map_err(|_| {
        HttpFault::new(
            "CAPABILITY_UPSTREAM_INVALID_RESPONSE",
            "Connector response could not be read.",
        )
    })?;
    if bytes.len() > 1_048_576 {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_INVALID_RESPONSE",
            "Connector response exceeds 1 MiB.",
        ));
    }
    let result: Value = serde_json::from_slice(&bytes).map_err(|_| {
        HttpFault::new(
            "CAPABILITY_UPSTREAM_INVALID_RESPONSE",
            "Connector response must be a UTF-8 JSON document.",
        )
    })?;
    if !result.is_object() {
        return Err(HttpFault::new(
            "CAPABILITY_UPSTREAM_INVALID_RESPONSE",
            "Connector response must be a JSON object.",
        ));
    }
    Ok(result)
}

/// Whether a plugin manifest carries per-capability HTTP operations.
pub fn has_operations(manifest: &Value) -> bool {
    manifest
        .get("http")
        .and_then(|http| http.get("operations"))
        .and_then(Value::as_object)
        .map(|operations| !operations.is_empty())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_manifest_without_operations_is_not_operation_driven() {
        assert!(!has_operations(
            &json!({"http": {"base_url": "https://example.com"}})
        ));
        assert!(has_operations(&json!({
            "http": {"operations": {"ping": {"method": "GET", "path": "/ping"}}}
        })));
    }

    #[test]
    fn path_components_are_escaped() {
        assert_eq!(encode_component("a/b c"), "a%2Fb%20c");
    }
}
