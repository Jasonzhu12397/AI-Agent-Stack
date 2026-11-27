#![allow(clippy::result_large_err)]

use std::collections::BTreeSet;

use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Map, Value};

use crate::proxy::AppState;

const WORKFLOW_PATH: &str = ".github/workflows/metal3-lab-e2e.yml";
const REQUIRED_ACCEPTANCE_LABELS: [&str; 3] = ["self-hosted", "metal3-lab", "physical"];
const ACCEPTANCE_JOB_NAMES: [&str; 2] = ["acceptance", "lab-acceptance"];
const KUBECONFIG_SECRETS: [&str; 2] = ["EPH_KUBECONFIG", "TARGET_KUBECONFIG"];

fn check(name: &str, ok: bool, detail: String) -> Value {
    json!({"name": name, "ok": ok, "detail": detail})
}

fn labels(job: &Map<String, Value>) -> Vec<String> {
    match job.get("runs-on") {
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| item.to_string().trim_matches('"').to_string())
            .collect(),
        Some(Value::String(value)) => value
            .split(',')
            .map(|part| part.trim().to_string())
            .filter(|part| !part.is_empty())
            .collect(),
        _ => Vec::new(),
    }
}

fn secret_names(document: &Value) -> Vec<String> {
    fn walk(value: &Value, names: &mut BTreeSet<String>) {
        match value {
            Value::Object(map) => {
                for (key, item) in map {
                    walk(&Value::String(key.clone()), names);
                    walk(item, names);
                }
            }
            Value::Array(items) => {
                for item in items {
                    walk(item, names);
                }
            }
            Value::String(text) => {
                for chunk in text.split("secrets.").skip(1) {
                    let name: String = chunk
                        .chars()
                        .take_while(|ch| ch.is_ascii_alphanumeric() || *ch == '_')
                        .collect();
                    if !name.is_empty() {
                        names.insert(name);
                    }
                }
            }
            _ => {}
        }
    }
    let mut names = BTreeSet::new();
    walk(document, &mut names);
    names.into_iter().collect()
}

async fn fetch_last_run(repository: &str, token: &str) -> Option<Value> {
    if repository.is_empty() || !repository.contains('/') || token.is_empty() {
        return None;
    }
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .ok()?;
    let encoded = WORKFLOW_PATH.replace('/', "%2F");
    let url = format!(
        "https://api.github.com/repos/{repository}/actions/workflows/{encoded}/runs?per_page=1"
    );
    let response = client
        .get(url)
        .header("accept", "application/vnd.github+json")
        .header("authorization", format!("Bearer {token}"))
        .header("user-agent", "ai-agent-stack-platform")
        .send()
        .await
        .ok()?;
    let payload: Value = response.json().await.ok()?;
    let run = payload.get("workflow_runs")?.as_array()?.first()?.clone();
    Some(json!({
        "status": run.get("status").cloned().unwrap_or(Value::Null),
        "conclusion": run.get("conclusion").cloned().unwrap_or(Value::Null),
        "run_number": run.get("run_number").cloned().unwrap_or(Value::Null),
        "event": run.get("event").cloned().unwrap_or(Value::Null),
        "created_at": run.get("created_at").cloned().unwrap_or(Value::Null),
        "updated_at": run.get("updated_at").cloned().unwrap_or(Value::Null),
        "html_url": run.get("html_url").cloned().unwrap_or(Value::Null),
    }))
}

/// Native replacement for `GET /api/v1/metal3/lab-workflow/`.
pub async fn lab_workflow(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(response) = crate::billing_api::require_user(&state, &headers).await {
        return response;
    }
    let path = std::env::current_dir()
        .unwrap_or_default()
        .join(WORKFLOW_PATH);
    let mut checks = Vec::new();
    let mut result = json!({
        "available": false,
        "workflow": WORKFLOW_PATH,
        "checks": checks,
        "dispatch_inputs": [],
        "secrets_referenced": [],
        "environment": null,
        "ready": false,
        "secret_presence": "not_observable",
        "reason": null,
    });
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(_) => {
            checks.push(check(
                "workflow.present",
                false,
                format!("{WORKFLOW_PATH} is not deployed here"),
            ));
            result["checks"] = Value::Array(checks);
            result["reason"] = json!("LAB_WORKFLOW_NOT_PRESENT");
            return finish(result).await;
        }
    };
    let document: Value = match serde_yaml::from_str(&text) {
        Ok(Value::Object(map)) => Value::Object(map),
        _ => {
            checks.push(check(
                "workflow.parse",
                false,
                "workflow is not valid YAML".into(),
            ));
            result["checks"] = Value::Array(checks);
            result["reason"] = json!("LAB_WORKFLOW_INVALID");
            return finish(result).await;
        }
    };
    let Some(document) = document.as_object().cloned() else {
        checks.push(check(
            "workflow.parse",
            false,
            "workflow is not a mapping".into(),
        ));
        result["checks"] = Value::Array(checks);
        result["reason"] = json!("LAB_WORKFLOW_INVALID");
        return finish(result).await;
    };
    result["available"] = json!(true);
    checks.push(check("workflow.present", true, WORKFLOW_PATH.into()));
    let triggers = document.get("on").or_else(|| document.get("true"));
    let inputs = triggers
        .and_then(|value| value.get("workflow_dispatch"))
        .and_then(|value| value.get("inputs"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let input_names: Vec<String> = inputs.keys().cloned().collect();
    result["dispatch_inputs"] = json!(input_names);
    checks.push(check(
        "workflow.dispatch_inputs",
        !input_names.is_empty(),
        if input_names.is_empty() {
            "no workflow_dispatch inputs declared".into()
        } else {
            input_names.join(", ")
        },
    ));
    let jobs = document
        .get("jobs")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let readiness = jobs
        .get("lab-readiness")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let acceptance = ACCEPTANCE_JOB_NAMES
        .iter()
        .find_map(|name| jobs.get(*name).and_then(Value::as_object));
    let readiness_outputs: Vec<String> = readiness
        .get("outputs")
        .and_then(Value::as_object)
        .map(|outputs| outputs.keys().cloned().collect())
        .unwrap_or_default();
    result["readiness_job"] = json!({
        "name": readiness.get("name").cloned().unwrap_or(Value::Null),
        "runs_on": labels(&readiness),
        "environment": readiness.get("environment").cloned().unwrap_or(Value::Null),
        "outputs": readiness_outputs,
    });
    let acceptance_labels = acceptance.map(labels).unwrap_or_default();
    result["acceptance_job"] = json!({
        "name": acceptance.and_then(|job| job.get("name")).cloned().unwrap_or(Value::Null),
        "runs_on": acceptance_labels,
        "needs": acceptance.and_then(|job| job.get("needs")).cloned().unwrap_or(Value::Null),
        "environment": acceptance.and_then(|job| job.get("environment")).cloned().unwrap_or(Value::Null),
    });
    result["environment"] = readiness.get("environment").cloned().unwrap_or(Value::Null);
    let missing: Vec<&str> = REQUIRED_ACCEPTANCE_LABELS
        .iter()
        .copied()
        .filter(|label| !acceptance_labels.iter().any(|item| item == label))
        .collect();
    let physical_ok = !acceptance_labels.is_empty() && missing.is_empty();
    let physical_detail = if physical_ok {
        format!("runs-on {}", acceptance_labels.join(", "))
    } else {
        format!(
            "missing label(s): {}",
            if missing.is_empty() {
                "runs-on not declared".to_string()
            } else {
                missing.join(", ")
            }
        )
    };
    checks.push(check(
        "acceptance.physical_runner",
        physical_ok,
        physical_detail,
    ));
    let needs_list: Vec<String> = match acceptance.and_then(|job| job.get("needs")) {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
        Some(Value::String(value)) => vec![value.clone()],
        _ => Vec::new(),
    };
    let gate = needs_list.iter().any(|item| item == "lab-readiness");
    checks.push(check(
        "acceptance.readiness_gate",
        gate,
        if gate {
            "needs lab-readiness".into()
        } else {
            "acceptance is not gated on the readiness job".into()
        },
    ));
    let job_when = acceptance
        .and_then(|job| job.get("if"))
        .map(|value| match value {
            Value::String(text) => text.clone(),
            other => other.to_string(),
        })
        .unwrap_or_default();
    let gated_on_ready = job_when.contains("ready") && job_when.contains("true");
    checks.push(check(
        "acceptance.ready_condition",
        gated_on_ready,
        if job_when.is_empty() {
            "acceptance job runs unconditionally".into()
        } else {
            job_when.clone()
        },
    ));
    let secrets = secret_names(&Value::Object(document.clone()));
    result["secrets_referenced"] = json!(secrets);
    let missing_secrets: Vec<&str> = KUBECONFIG_SECRETS
        .iter()
        .copied()
        .filter(|name| !secrets.iter().any(|item| item == name))
        .collect();
    let secrets_ok = missing_secrets.is_empty();
    checks.push(check(
        "workflow.kubeconfig_secrets",
        secrets_ok,
        if secrets_ok {
            format!("references {}", secrets.join(", "))
        } else {
            format!("missing reference(s): {}", missing_secrets.join(", "))
        },
    ));
    result["ready"] = json!(checks
        .iter()
        .all(|item| item.get("ok").and_then(Value::as_bool).unwrap_or(false)));
    result["checks"] = Value::Array(checks);
    let repository = std::env::var("LAB_WORKFLOW_REPOSITORY")
        .unwrap_or_default()
        .trim()
        .to_string();
    let token = std::env::var("LAB_WORKFLOW_TOKEN")
        .unwrap_or_default()
        .trim()
        .to_string();
    let run = if repository.is_empty() || token.is_empty() {
        None
    } else {
        fetch_last_run(&repository, &token).await
    };
    if run.is_some() {
        result["secret_presence"] = json!("observed");
    }
    result["last_run"] = run.unwrap_or(Value::Null);
    finish(result).await
}

async fn finish(mut report: Value) -> Response {
    let workflow_url = std::env::var("METAL3_LAB_WORKFLOW_URL")
        .unwrap_or_default()
        .trim()
        .to_string();
    report["workflow_url"] = if workflow_url.is_empty() {
        Value::Null
    } else {
        json!(workflow_url)
    };
    report["last_run"] = report.get("last_run").cloned().unwrap_or(Value::Null);
    (StatusCode::OK, Json(report)).into_response()
}
