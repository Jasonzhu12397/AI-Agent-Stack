use std::time::Duration;

use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;
use crate::tenancy::is_postgres;

#[derive(Clone, Debug)]
pub struct RuntimeModel {
    pub id: String,
    pub provider_id: String,
    pub provider_name: String,
    pub provider_type: String,
    pub model_name: String,
    pub base_url: String,
    pub credential_ref: String,
    pub config: Value,
    pub capabilities: Vec<String>,
    pub max_output_tokens: i64,
}

#[derive(Clone, Debug)]
pub struct Completion {
    pub content: String,
    pub tool_calls: Vec<Value>,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub latency_ms: i64,
}

pub async fn load(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    user_id: i64,
    model_id: &str,
) -> Result<Option<RuntimeModel>, String> {
    let postgres = is_postgres(state);
    let true_literal = if postgres { "TRUE" } else { "1" };
    let config_text = if postgres {
        "CAST(provider.config AS TEXT)"
    } else {
        "provider.config"
    };
    let capabilities = if postgres {
        "CAST(config.capabilities AS TEXT)"
    } else {
        "config.capabilities"
    };
    let sql = format!(
        "SELECT CAST(config.id AS TEXT) AS id, CAST(provider.id AS TEXT) AS provider_id, \
         provider.name AS provider_name, provider.provider_type, provider.base_url, \
         provider.credential_ref, config.model_name, config.max_output_tokens, \
         {config_text} AS config_text, {capabilities} AS capabilities \
         FROM core_modelconfig config JOIN core_modelprovider provider \
           ON CAST(provider.id AS TEXT) = CAST(config.provider_id AS TEXT) \
         WHERE CAST(config.id AS TEXT) = ? AND config.enabled = {true_literal} \
           AND provider.enabled = {true_literal} \
           AND (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
           AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?)"
    );
    let row = sqlx::query(&sql)
        .bind(model_id)
        .bind(tenant_id)
        .bind(user_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok(None);
    };
    Ok(Some(RuntimeModel {
        id: row.try_get("id").unwrap_or_default(),
        provider_id: row.try_get("provider_id").unwrap_or_default(),
        provider_name: row.try_get("provider_name").unwrap_or_default(),
        provider_type: row.try_get("provider_type").unwrap_or_default(),
        model_name: row.try_get("model_name").unwrap_or_default(),
        base_url: row.try_get("base_url").unwrap_or_default(),
        credential_ref: row.try_get("credential_ref").unwrap_or_default(),
        config: row
            .try_get::<Option<String>, _>("config_text")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({})),
        capabilities: parse_strings(
            row.try_get::<Option<String>, _>("capabilities")
                .ok()
                .flatten(),
        ),
        max_output_tokens: row.try_get::<i64, _>("max_output_tokens").unwrap_or(1024),
    }))
}

fn parse_strings(value: Option<String>) -> Vec<String> {
    value
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.as_array().cloned())
        .map(|items| {
            items
                .into_iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

pub async fn credential(
    pool: &AnyPool,
    state: &AppState,
    model: &RuntimeModel,
    user_id: i64,
) -> Result<String, String> {
    let mut reference = model.credential_ref.clone();
    if reference.starts_with("member://") {
        let secret_id = reference.trim_start_matches("member://");
        let sealed = sqlx::query_scalar::<_, String>(
            "SELECT sealed_value FROM core_membercredentialsecret WHERE id = ? AND user_id = ?",
        )
        .bind(secret_id)
        .bind(user_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
        let Some(sealed) = sealed else {
            return Err("Saved model credential is unavailable.".into());
        };
        let key =
            state.config.platform_secret_key.as_deref().ok_or_else(|| {
                "PLATFORM_SECRET_KEY is required to unseal credentials.".to_string()
            })?;
        return crate::model_api::unseal_value(key, &sealed, "member-credential-secret")
            .map_err(|_| "Saved model credential could not be decrypted.".to_string());
    }
    if let Some(name) = reference
        .strip_prefix("env://")
        .or_else(|| reference.strip_prefix("env:"))
    {
        reference = std::env::var(name).unwrap_or_default();
    }
    Ok(reference)
}

impl RuntimeModel {
    pub async fn complete(
        &self,
        state: &AppState,
        credential: &str,
        messages: &[Value],
        max_tokens: i64,
        tools: Option<&[Value]>,
        timeout_seconds: i64,
    ) -> Result<Completion, String> {
        if self.provider_type == "fake" {
            return Ok(self.fake_completion(messages, tools));
        }
        if self.provider_type != "openai_compatible" {
            return Err("The selected provider does not support chat.".into());
        }
        if !self.capabilities.iter().any(|value| value == "chat") {
            return Err("The selected model does not declare the chat capability.".into());
        }
        let url = format!("{}/chat/completions", self.base_url.trim_end_matches('/'));
        let mut body = json!({
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens.max(1),
        });
        if let Some(tools) = tools {
            if !tools.is_empty() {
                body["tools"] = json!(tools);
                body["tool_choice"] = json!("auto");
            }
        }
        let mut request = state
            .client
            .post(&url)
            .header("Accept", "application/json")
            .timeout(Duration::from_secs(timeout_seconds.clamp(1, 120) as u64))
            .json(&body);
        if !credential.is_empty() {
            request = request.header("Authorization", format!("Bearer {credential}"));
        }
        let started = std::time::Instant::now();
        let response = request
            .send()
            .await
            .map_err(|_| "The model provider could not be reached.".to_string())?;
        if !response.status().is_success() {
            return Err(format!(
                "The model provider refused the request (HTTP {}).",
                response.status().as_u16()
            ));
        }
        let document: Value = response
            .json()
            .await
            .map_err(|_| "The model provider returned unreadable JSON.".to_string())?;
        let message = document
            .pointer("/choices/0/message")
            .cloned()
            .ok_or_else(|| "The model provider returned no message.".to_string())?;
        let content = message
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let tool_calls = sanitize_tool_calls(message.get("tool_calls").cloned());
        if content.is_empty() && tool_calls.is_empty() {
            return Err("The model provider returned an empty response.".to_string());
        }
        Ok(Completion {
            content,
            tool_calls,
            input_tokens: document
                .pointer("/usage/prompt_tokens")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .max(0),
            output_tokens: document
                .pointer("/usage/completion_tokens")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .max(0),
            latency_ms: started.elapsed().as_millis() as i64,
        })
    }

    fn fake_completion(&self, messages: &[Value], tools: Option<&[Value]>) -> Completion {
        let intent = messages
            .iter()
            .rev()
            .find(|message| message.get("role").and_then(Value::as_str) == Some("user"))
            .and_then(|message| message.get("content"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let content = self
            .config
            .get("answer")
            .or_else(|| self.config.get("response"))
            .and_then(Value::as_str)
            .unwrap_or("Fake model response")
            .to_string();
        let mut tool_calls = Vec::new();
        if self.config.get("tool_call").and_then(Value::as_bool) == Some(true) {
            if let Some(tools) = tools {
                if !messages
                    .iter()
                    .any(|message| message.get("role").and_then(Value::as_str) == Some("tool"))
                {
                    if let Some(tool) = tools.iter().find(|tool| {
                        let name = tool
                            .pointer("/function/name")
                            .and_then(Value::as_str)
                            .unwrap_or("");
                        !name.is_empty() && intent.to_lowercase().contains(&name.to_lowercase())
                    }) {
                        let name = tool
                            .pointer("/function/name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string();
                        tool_calls.push(json!({
                            "id": "call_fake_0",
                            "type": "function",
                            "function": {"name": name, "arguments": "{}"},
                        }));
                    }
                }
            }
        }
        Completion {
            content: if tool_calls.is_empty() {
                content
            } else {
                String::new()
            },
            tool_calls,
            input_tokens: 0,
            output_tokens: 0,
            latency_ms: 0,
        }
    }

    pub async fn embed(
        &self,
        state: &AppState,
        credential: &str,
        inputs: &[String],
    ) -> Result<Vec<Vec<f64>>, String> {
        if self.provider_type != "openai_compatible" {
            return Err("The selected provider does not support embeddings.".into());
        }
        if !self.capabilities.iter().any(|value| value == "embeddings") {
            return Err("The selected model does not declare the embeddings capability.".into());
        }
        if inputs.is_empty() || inputs.len() > 128 {
            return Err("Embedding requests require 1 to 128 inputs.".into());
        }
        let url = format!("{}/embeddings", self.base_url.trim_end_matches('/'));
        let mut request = state
            .client
            .post(&url)
            .header("Accept", "application/json")
            .timeout(Duration::from_secs(45))
            .json(&json!({"model": self.model_name, "input": inputs}));
        if !credential.is_empty() {
            request = request.header("Authorization", format!("Bearer {credential}"));
        }
        let response = request
            .send()
            .await
            .map_err(|_| "The embedding provider could not be reached.".to_string())?;
        if !response.status().is_success() {
            return Err(format!(
                "The embedding provider refused the request (HTTP {}).",
                response.status().as_u16()
            ));
        }
        let document: Value = response
            .json()
            .await
            .map_err(|_| "The embedding provider returned unreadable JSON.".to_string())?;
        let data = document
            .get("data")
            .and_then(Value::as_array)
            .ok_or_else(|| "The embedding provider returned no vectors.".to_string())?;
        let mut rows = Vec::new();
        for (index, row) in data.iter().enumerate() {
            rows.push((
                row.get("index")
                    .and_then(Value::as_i64)
                    .unwrap_or(index as i64),
                row.get("embedding")
                    .and_then(Value::as_array)
                    .ok_or_else(|| "An embedding vector is invalid.".to_string())?
                    .iter()
                    .map(|value| value.as_f64().unwrap_or(f64::NAN))
                    .collect::<Vec<_>>(),
            ));
        }
        rows.sort_by_key(|row| row.0);
        if rows.len() != inputs.len() {
            return Err("The embedding provider returned the wrong number of vectors.".into());
        }
        let dimensions = rows.first().map(|row| row.1.len()).unwrap_or(0);
        if dimensions == 0
            || rows.iter().any(|row| row.1.len() != dimensions)
            || rows
                .iter()
                .any(|row| row.1.iter().any(|value| !value.is_finite()))
        {
            return Err("The embedding provider returned invalid dimensions.".into());
        }
        Ok(rows.into_iter().map(|row| row.1).collect())
    }
}

fn sanitize_tool_calls(value: Option<Value>) -> Vec<Value> {
    value
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter(|call| {
            call.get("id").and_then(Value::as_str).is_some()
                && call
                    .pointer("/function/name")
                    .and_then(Value::as_str)
                    .is_some()
        })
        .collect()
}
