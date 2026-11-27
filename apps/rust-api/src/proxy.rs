use std::net::SocketAddr;
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, Method, Request, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, patch, post};
use axum::{Json, Router};
use futures_util::TryStreamExt;
use serde_json::json;
use tokio::sync::Semaphore;
use tower_http::limit::RequestBodyLimitLayer;

use crate::config::Config;

const MAX_WEBSOCKET_CONNECTIONS: usize = 256;

const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

#[derive(Clone)]
pub struct AppState {
    pub config: Config,
    pub client: reqwest::Client,
    pub identity_pool: Option<sqlx::AnyPool>,
    pub websocket_slots: Arc<Semaphore>,
}

impl AppState {
    pub fn new(config: Config) -> Result<Self, String> {
        let client = reqwest::Client::builder()
            .timeout(config.request_timeout)
            .build()
            .map_err(|error| format!("failed to build the upstream HTTP client: {error}"))?;
        let identity_pool = match &config.database_url {
            Some(url) => {
                sqlx::any::install_default_drivers();
                Some(
                    sqlx::AnyPool::connect_lazy(url)
                        .map_err(|error| format!("DATABASE_URL is invalid: {error}"))?,
                )
            }
            None => None,
        };
        Ok(Self {
            config,
            client,
            identity_pool,
            websocket_slots: Arc::new(Semaphore::new(MAX_WEBSOCKET_CONNECTIONS)),
        })
    }
}

pub fn router(state: AppState) -> Router {
    let native_identity = state.config.native_identity;
    let mut router = Router::new()
        .route("/health/ready", get(health))
        .route("/api/v1/edge/info", get(info))
        .route("/ws/{*path}", get(crate::ws::forward));
    if native_identity {
        router = router
            .route("/api/v1/auth/csrf/", get(crate::auth::csrf))
            .route("/api/v1/auth/login/", post(crate::auth::login))
            .route("/api/v1/auth/logout/", post(crate::auth::logout))
            .route("/api/v1/auth/me/", get(crate::native::current_user))
            .route("/api/v1/platform/roadmap/", get(crate::platform_api::roadmap))
            .route(
                "/api/v1/agent-runtime/heartbeat/",
                axum::routing::post(crate::agent_runtime_api::heartbeat),
            )
            .route(
                "/api/v1/agent-runtime/commands/{command_id}/lease/renew/",
                axum::routing::post(crate::agent_runtime_api::renew_lease),
            )
            .route(
                "/api/v1/agent-nodes/{node_id}/client-certificate/",
                axum::routing::put(crate::agent_runtime_api::bound_client_certificate)
                    .delete(crate::agent_runtime_api::bound_client_certificate),
            )
            .route(
                "/api/v1/agent-nodes/{node_id}/revoke/",
                axum::routing::post(crate::agent_runtime_api::revoke_agent_node),
            )
            .route(
                "/api/v1/agent-nodes/{node_id}/credentials/rotate/",
                axum::routing::post(crate::agent_runtime_api::rotate_agent_credential),
            )
            .route(
                "/api/v1/agent-plugins/{plugin_id}/",
                axum::routing::get(crate::plugin_api::plugin_detail)
                    .post(crate::plugin_api::plugin_detail),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-teams/",
                axum::routing::get(crate::agent_team_api::list_teams)
                    .post(crate::agent_team_api::create_team),
            )
            .route(
                "/api/v1/agent-teams/{team_id}/",
                axum::routing::get(crate::agent_team_api::team_detail)
                    .patch(crate::agent_team_api::team_detail),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/archive-items/",
                axum::routing::get(crate::archive_api::list_archive_items),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/archive-items/{item_type}/{item_id}/restore/",
                axum::routing::post(crate::archive_api::restore_archive_item),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/archive-items/{item_type}/{item_id}/",
                axum::routing::delete(crate::archive_api::purge_archive_item),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/cloud-vms/providers/",
                axum::routing::get(crate::cloud_vm_api::list_providers),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/cloud-vms/",
                axum::routing::get(crate::cloud_vm_api::list_instances),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/marketplace-agents/",
                axum::routing::get(crate::plugin_api::list_marketplace_agents),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-capabilities/",
                axum::routing::get(crate::plugin_api::list_capabilities),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/fleet/network/collect/",
                axum::routing::post(crate::agent_runtime_api::collect_fleet_network),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-commands/",
                axum::routing::get(crate::agent_runtime_api::list_project_commands),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/privacy/deletion-requests/",
                axum::routing::get(crate::privacy_api::deletion_requests)
                    .post(crate::privacy_api::deletion_requests),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/privacy/grants/",
                axum::routing::get(crate::privacy_api::list_grants),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/privacy/grants/{grant_id}/",
                axum::routing::delete(crate::privacy_api::revoke_grant),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-nodes/",
                axum::routing::get(crate::agent_runtime_api::list_agent_nodes)
                    .post(crate::agent_runtime_api::create_agent_node),
            )
            .route(
                "/api/v1/agent-nodes/{node_id}/commands/",
                axum::routing::get(crate::agent_runtime_api::list_node_commands),
            )
            .route(
                "/api/v1/agent-commands/{command_id}/approve/",
                axum::routing::post(crate::agent_runtime_api::approve_command),
            )
            .route(
                "/api/v1/agent-runtime/commands/poll/",
                axum::routing::post(crate::agent_runtime_api::poll_commands),
            )
            .route(
                "/api/v1/agent-runtime/commands/{command_id}/result/",
                axum::routing::post(crate::agent_runtime_api::command_result),
            )
            .route(
                "/api/v1/tenants/",
                get(crate::tenancy::list_tenants).post(crate::tenancy::create_tenant),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/",
                get(crate::tenancy::list_projects).post(crate::tenancy::create_project),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/members/",
                get(crate::tenancy::list_tenant_members).post(crate::tenancy::create_tenant_member),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/members/{membership_id}/",
                patch(crate::tenancy::update_tenant_member)
                    .delete(crate::tenancy::delete_tenant_member),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/members/",
                get(crate::tenancy::list_project_members)
                    .post(crate::tenancy::create_project_member),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/members/{membership_id}/",
                patch(crate::tenancy::update_project_member)
                    .delete(crate::tenancy::delete_project_member),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/tasks/",
                get(crate::tasks::list_tasks).post(crate::tasks::create_task),
            )
            .route("/api/v1/tasks/{task_id}/", get(crate::tasks::get_task))
            .route(
                "/api/v1/tasks/{task_id}/cancel/",
                post(crate::tasks::cancel_task),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/audit-events/",
                get(crate::audit_api::list_tenant_events),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/audit-events/verify/",
                get(crate::audit_api::verify_tenant_events),
            )
            .route(
                "/api/v1/audit-events/global/",
                get(crate::audit_api::list_global_events),
            )
            .route(
                "/api/v1/audit-events/global/verify/",
                get(crate::audit_api::verify_global_events),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/token-budget/",
                get(crate::token_budget::get_budget)
                    .put(crate::token_budget::update_budget)
                    .patch(crate::token_budget::patch_budget),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/workflows/",
                get(crate::workflows::list_workflows).post(crate::workflows::create_workflow),
            )
            .route(
                "/api/v1/workflows/{workflow_id}/",
                get(crate::workflows::get_workflow).patch(crate::workflows::update_workflow),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/workflow-catalog/",
                get(crate::workflow_reads::catalog),
            )
            .route(
                "/api/v1/workflows/{workflow_id}/validate/",
                post(crate::workflows::validate_workflow),
            )
            .route(
                "/api/v1/workflows/{workflow_id}/publish/",
                post(crate::workflows::publish_workflow),
            )
            .route(
                "/api/v1/workflows/{workflow_id}/runs/",
                get(crate::workflow_reads::list_runs).post(crate::workflow_reads::create_run),
            )
            .route(
                "/api/v1/workflow-runs/{run_id}/",
                get(crate::workflow_steps::get_run),
            )
            .route(
                "/api/v1/workflow-runs/{run_id}/steps/{node_id}/approve/",
                post(crate::workflow_steps::approve),
            )
            .route(
                "/api/v1/workflow-runs/{run_id}/steps/{node_id}/reject/",
                post(crate::workflow_steps::reject),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/model-providers/",
                get(crate::model_api::list_providers).post(crate::model_api::create_provider),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/model-providers/catalog/",
                get(crate::model_api::provider_catalog),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/model-providers/{provider_id}/account/",
                get(crate::model_api::get_provider_account)
                    .post(crate::model_api::link_provider_account)
                    .delete(crate::model_api::delete_provider_account),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/model-providers/oauth/{preset_key}/start/",
                post(crate::model_api::oauth_start),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/model-providers/oauth/{preset_key}/exchange/",
                post(crate::model_api::oauth_exchange),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/model-providers/{provider_id}/models/",
                get(crate::model_api::list_configs).post(crate::model_api::create_config),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/model-configs/{model_config_id}/",
                axum::routing::patch(crate::model_api::update_config),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/model-configs/{model_config_id}/test/",
                axum::routing::post(crate::chat_api::test_model),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agents/",
                get(crate::agent_api::list_agents).post(crate::agent_api::create_agent),
            )
            .route(
                "/api/v1/agents/{agent_id}/",
                get(crate::agent_api::get_agent).patch(crate::agent_api::update_agent),
            )
            .route(
                "/api/v1/agents/{agent_id}/publish/",
                post(crate::agent_api::publish_agent),
            )
            .route(
                "/api/v1/agents/{agent_id}/runs/",
                get(crate::agent_api::list_agent_runs).post(crate::agent_api::create_agent_run),
            )
            .route(
                "/api/v1/runs/{run_id}/",
                get(crate::agent_api::get_agent_run),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/plans/",
                get(crate::billing_api::list_plans),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/subscription/",
                get(crate::billing_api::get_subscription),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/usage/",
                get(crate::billing_api::get_usage),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/capabilities/",
                get(crate::billing_api::get_capabilities),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/orders/",
                get(crate::billing_api::list_orders).post(crate::billing_api::create_order),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/orders/{order_id}/",
                get(crate::billing_api::get_order),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/orders/{order_id}/status/",
                get(crate::billing_api::get_order_status),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/orders/{order_id}/close/",
                axum::routing::post(crate::billing_api::close_order),
            )
            .route(
                "/api/v1/users/me/",
                get(crate::account_api::get_profile).patch(crate::account_api::update_profile),
            )
            .route(
                "/api/v1/users/me/identities/",
                get(crate::account_api::list_identities),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/privacy/sessions/",
                get(crate::account_api::list_privacy_sessions),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/privacy/sessions/{session_key}/revoke/",
                axum::routing::post(crate::account_api::revoke_privacy_session),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/settings/{section}/",
                get(crate::settings_api::tenant_get).patch(crate::settings_api::tenant_patch),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/scheduled-jobs/",
                get(crate::scheduled_api::list_jobs).post(crate::scheduled_api::create_job),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/scheduled-jobs/{job_id}/",
                axum::routing::patch(crate::scheduled_api::update_job)
                    .delete(crate::scheduled_api::delete_job),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/scheduled-jobs/{job_id}/run-now/",
                axum::routing::post(crate::scheduled_api::run_job_now),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/settings/{section}/",
                get(crate::settings_api::project_get).patch(crate::settings_api::project_patch),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/settings/notifications/endpoints/",
                get(crate::notification_api::list_endpoints)
                    .post(crate::notification_api::create_endpoint),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/settings/notifications/endpoints/{endpoint_id}/",
                axum::routing::patch(crate::notification_api::update_endpoint)
                    .delete(crate::notification_api::delete_endpoint),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/settings/notifications/endpoints/{endpoint_id}/test/",
                axum::routing::post(crate::notification_api::test_endpoint),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/feedback/",
                get(crate::preferences_api::list_feedback)
                    .post(crate::preferences_api::create_feedback),
            )
            .route(
                "/api/v1/meta/shortcuts/",
                get(crate::preferences_api::shortcuts),
            )
            .route("/api/v1/meta/about/", get(crate::preferences_api::about))
            .route(
                "/api/v1/users/me/preferences/{namespace}/",
                get(crate::preferences_api::get_preferences)
                    .put(crate::preferences_api::update_preferences)
                    .patch(crate::preferences_api::update_preferences),
            )
            .route(
                "/api/v1/users/me/sessions/",
                get(crate::account_api::list_sessions),
            )
            .route(
                "/api/v1/users/me/sessions/{session_key}/revoke/",
                axum::routing::post(crate::account_api::revoke_session),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/subscription/trial/",
                axum::routing::post(crate::billing_api::start_subscription_trial),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/subscription/cancel/",
                axum::routing::post(crate::billing_api::cancel_subscription),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/subscription/resume/",
                axum::routing::post(crate::billing_api::resume_subscription),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/subscription/renew/",
                axum::routing::post(crate::billing_api::renew_subscription),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/subscription/settle/",
                axum::routing::post(crate::billing_api::settle_subscriptions),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/billing/payments/credentials/{channel}/",
                get(crate::billing_api::get_payment_credentials)
                    .put(crate::billing_api::put_payment_credentials),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-chat/threads/",
                get(crate::chat_api::list_threads).post(crate::chat_api::create_thread),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-chat/context/",
                get(crate::chat_api::chat_context),
            )
            .route(
                "/api/v1/agent-chat/threads/{thread_id}/messages/",
                get(crate::chat_api::list_messages).post(crate::chat_api::create_message),
            )
            .route(
                "/api/v1/agent-chat/threads/{thread_id}/attachments/",
                get(crate::chat_api::list_attachments).post(crate::chat_api::upload_attachment),
            )
            .route(
                "/api/v1/agent-chat/attachments/{attachment_id}/",
                get(crate::chat_api::download_attachment)
                    .delete(crate::chat_api::delete_attachment),
            )
            .route(
                "/api/v1/agent-chat/threads/{thread_id}/",
                get(crate::chat_api::get_thread)
                    .patch(crate::chat_api::update_thread)
                    .delete(crate::chat_api::delete_thread),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/marketplace/catalog/",
                get(crate::plugin_api::marketplace_catalog),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/marketplace/catalog/{plugin_key}/",
                get(crate::plugin_api::marketplace_catalog_detail),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/agent-plugins/",
                get(crate::plugin_api::list_plugins),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-plugin-installations/",
                get(crate::plugin_api::list_installations),
            )
            .route(
                "/api/v1/agent-plugin-installations/{installation_id}/status/",
                post(crate::plugin_api::update_installation_status),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/knowledge-bases/",
                get(crate::knowledge_api::list_knowledge_bases)
                    .post(crate::knowledge_api::create_knowledge_base),
            )
            .route(
                "/api/v1/knowledge-bases/{knowledge_base_id}/documents/",
                get(crate::knowledge_api::list_documents)
                    .post(crate::knowledge_api::create_document),
            )
            .route(
                "/api/v1/knowledge-documents/{document_id}/",
                axum::routing::delete(crate::knowledge_api::delete_document),
            )
            .route(
                "/api/v1/knowledge-documents/{document_id}/retry/",
                axum::routing::post(crate::knowledge_api::retry_document),
            )
            .route("/health/live", get(edge_liveness));
    }
    router
        .fallback(forward)
        .layer(RequestBodyLimitLayer::new(25 * 1024 * 1024))
        .layer(middleware::from_fn(csrf_middleware))
        .with_state(state)
}

async fn csrf_middleware(request: Request<Body>, next: Next) -> Response {
    let method = request.method();
    let is_mutating = method == Method::POST
        || method == Method::PUT
        || method == Method::PATCH
        || method == Method::DELETE;
    let headers = request.headers();
    let has_session_cookie = headers
        .get("cookie")
        .and_then(|value| value.to_str().ok())
        .is_some_and(|cookie| {
            cookie.split(';').any(|pair| {
                pair.trim()
                    .split_once('=')
                    .is_some_and(|(name, _)| name.trim() == "sessionid")
            })
        });
    let uses_session_header = headers
        .get("x-session-token")
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| !value.trim().is_empty());

    if is_mutating && has_session_cookie && !uses_session_header {
        if let Err(response) = crate::auth::enforce_csrf(headers) {
            return *response;
        }
    }
    next.run(request).await
}

async fn edge_liveness(State(state): State<AppState>) -> Response {
    Json(json!({
        "status": "ok",
        "service": "rust-api-edge",
        "native_identity": state.config.native_identity,
    }))
    .into_response()
}

async fn health(State(state): State<AppState>) -> Response {
    let started = std::time::Instant::now();
    match state
        .client
        .get(state.config.upstream_health_url())
        .timeout(state.config.upstream_timeout)
        .send()
        .await
    {
        Ok(response) if response.status().is_success() => Json(json!({
            "status": "ready",
            "service": "rust-api-edge",
            "upstream_status": response.status().as_u16(),
            "latency_ms": started.elapsed().as_millis() as u64,
        }))
        .into_response(),
        Ok(response) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({
                "status": "degraded",
                "service": "rust-api-edge",
                "upstream_status": response.status().as_u16(),
            })),
        )
            .into_response(),
        Err(error) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "status": "unavailable",
                "service": "rust-api-edge",
                "error": error.to_string(),
            })),
        )
            .into_response(),
    }
}

#[derive(serde::Serialize)]
struct EdgeInfo {
    service: &'static str,
    version: &'static str,
    upstream: String,
    websocket: bool,
    server_sent_events: bool,
}

async fn info(State(state): State<AppState>) -> Json<EdgeInfo> {
    Json(EdgeInfo {
        service: "rust-api-edge",
        version: env!("CARGO_PKG_VERSION"),
        upstream: state
            .config
            .upstream
            .host_str()
            .unwrap_or("configured")
            .to_string(),
        websocket: true,
        server_sent_events: true,
    })
}

async fn forward(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    request: Request<Body>,
) -> Response {
    let method = request.method().clone();
    let uri = request.uri().clone();
    let incoming_headers = request.headers().clone();
    let body = request.into_body();

    let target = format!(
        "{}{}",
        state.config.upstream.as_str().trim_end_matches('/'),
        uri.path_and_query()
            .map(|value| value.as_str())
            .unwrap_or("/")
    );

    let mut builder = state.client.request(
        reqwest::Method::from_bytes(method.as_str().as_bytes()).unwrap_or(reqwest::Method::GET),
        &target,
    );

    let mut sanitized_headers = incoming_headers.clone();
    strip_hop_by_hop_headers(&mut sanitized_headers);

    let mut outbound = HeaderMap::new();
    for (name, value) in sanitized_headers.iter() {
        outbound.append(name, value.clone());
    }
    if !outbound.contains_key("x-request-id") {
        outbound.insert(
            HeaderName::from_static("x-request-id"),
            HeaderValue::from_str(&uuid::Uuid::new_v4().to_string())
                .unwrap_or_else(|_| HeaderValue::from_static("missing-request-id")),
        );
    }
    set_trusted_forwarded_headers(&mut outbound, peer, &state.config.external_scheme);
    builder = builder.headers(outbound);

    let content_length = incoming_headers
        .get("content-length")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok());
    if content_length.unwrap_or(0) > 0 || method != axum::http::Method::GET {
        builder = builder.body(reqwest::Body::wrap_stream(body.into_data_stream()));
    }

    let upstream_response = match builder.send().await {
        Ok(response) => response,
        Err(error) => {
            tracing::warn!(
                timeout = error.is_timeout(),
                connect = error.is_connect(),
                body = error.is_body(),
                "rust api edge could not reach the upstream"
            );
            return (
                StatusCode::BAD_GATEWAY,
                Json(json!({
                    "error": {
                        "code": "UPSTREAM_UNAVAILABLE",
                        "message": "The platform API could not be reached through the Rust edge.",
                    }
                })),
            )
                .into_response();
        }
    };

    let status = upstream_response.status();
    let mut headers = upstream_response.headers().clone();
    strip_hop_by_hop_headers(&mut headers);
    let mut response = Response::new(Body::from_stream(upstream_response.bytes_stream().map_err(
        |error| std::io::Error::other(format!("upstream response stream failed: {error}")),
    )));
    *response.status_mut() = status;
    for (name, value) in headers.iter() {
        response.headers_mut().append(name, value.clone());
    }
    response
}

pub(crate) fn set_trusted_forwarded_headers(
    headers: &mut HeaderMap,
    peer: SocketAddr,
    external_scheme: &str,
) {
    for name in [
        "forwarded",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-real-ip",
        "x-forwarded-for",
        "x-forwarded-proto",
    ] {
        headers.remove(name);
    }
    if let Ok(value) = HeaderValue::from_str(&peer.ip().to_string()) {
        headers.insert(HeaderName::from_static("x-forwarded-for"), value);
    }
    let scheme = if external_scheme == "https" {
        HeaderValue::from_static("https")
    } else {
        HeaderValue::from_static("http")
    };
    headers.insert(HeaderName::from_static("x-forwarded-proto"), scheme);
}

fn strip_hop_by_hop_headers(headers: &mut HeaderMap) {
    let connection_tokens: Vec<HeaderName> = headers
        .get_all("connection")
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .filter_map(|token| HeaderName::from_bytes(token.trim().as_bytes()).ok())
        .collect();

    for name in HOP_BY_HOP {
        headers.remove(*name);
    }
    for name in connection_tokens {
        headers.remove(name);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn removes_connection_nominated_headers() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "connection",
            HeaderValue::from_static("keep-alive, x-private-hop"),
        );
        headers.insert("keep-alive", HeaderValue::from_static("timeout=5"));
        headers.insert("x-private-hop", HeaderValue::from_static("internal"));
        headers.insert("x-forwarded-for", HeaderValue::from_static("192.0.2.10"));

        strip_hop_by_hop_headers(&mut headers);

        assert!(!headers.contains_key("connection"));
        assert!(!headers.contains_key("keep-alive"));
        assert!(!headers.contains_key("x-private-hop"));
        assert_eq!(headers.get("x-forwarded-for").unwrap(), "192.0.2.10");
    }
}
