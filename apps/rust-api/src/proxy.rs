use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, patch, post};
use axum::{Json, Router};
use serde_json::json;

use crate::config::Config;

#[derive(Clone)]
pub struct AppState {
    pub config: Config,
    pub identity_pool: Option<sqlx::AnyPool>,
    pub client: reqwest::Client,
}

impl AppState {
    pub fn new(config: Config) -> Result<Self, String> {
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
            identity_pool,
            client: reqwest::Client::new(),
        })
    }

    pub async fn migrate_identity(&self) -> Result<(), String> {
        let Some(pool) = self.identity_pool.as_ref() else {
            return Err("DATABASE_URL is required to initialize Rust identity storage".into());
        };
        for migration in [
            include_str!("../migrations/0001_rust_sessions.sql"),
            include_str!("../migrations/0002_platform_identity.sql"),
            include_str!("../migrations/0003_platform_user_profiles.sql"),
        ] {
            sqlx::raw_sql(migration)
                .execute(pool)
                .await
                .map_err(|error| format!("failed to apply Rust identity migration: {error}"))?;
        }
        Ok(())
    }
}

pub fn router(state: AppState) -> Router {
    let mut router = Router::new()
        .route("/health/ready", get(health))
        .route("/api/v1/edge/info", get(info));
    {
        router = router
            .route("/api/v1/auth/csrf/", get(crate::auth::csrf))
            .route("/api/v1/auth/register/", post(crate::auth::register))
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
                "/api/v1/agent-teams/{team_id}/publish/",
                axum::routing::post(crate::agent_team_api::publish_team),
            )
            .route(
                "/api/v1/agent-teams/{team_id}/runs/",
                axum::routing::get(crate::agent_team_api::list_team_runs),
            )
            .route(
                "/api/v1/agent-team-runs/{run_id}/",
                axum::routing::get(crate::agent_team_api::team_run_detail),
            )
            .route(
                "/api/v1/cluster-deployments/{deployment_id}/bmh-inventory/sync/",
                axum::routing::post(crate::cluster_sync_api::sync_bmh_inventory),
            )
            .route(
                "/api/v1/cluster-deployments/{deployment_id}/preflight/eph/sync/",
                axum::routing::post(crate::cluster_sync_api::sync_eph_preflight),
            )
            .route(
                "/api/v1/cluster-deployments/{deployment_id}/etcd-backup/",
                axum::routing::get(crate::etcd_api::get_state),
            )
            .route(
                "/api/v1/cluster-deployments/{deployment_id}/etcd-backup/policy/",
                axum::routing::post(crate::etcd_api::update_policy),
            )
            .route(
                "/api/v1/cluster-deployments/{deployment_id}/etcd-backup/run/",
                axum::routing::post(crate::etcd_api::run_backup),
            )
            .route(
                "/api/v1/cluster-deployments/{deployment_id}/etcd-backup/check/",
                axum::routing::post(crate::etcd_api::run_check),
            )
            .route(
                "/api/v1/cluster-deployments/{deployment_id}/network/evidence/verify/",
                axum::routing::post(crate::cluster_api::verify_network_evidence),
            )
            .route(
                "/api/v1/cluster-deployments/{deployment_id}/",
                axum::routing::get(crate::cluster_api::get_deployment),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/infrastructure-networks/",
                axum::routing::any(crate::infra_api::profile),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/cluster-deployments/ironic-template/",
                axum::routing::post(crate::cluster_sync_api::ironic_template),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/cluster-deployments/",
                axum::routing::get(crate::cluster_api::list_deployments),
            )
            .route(
                "/api/v1/metal3/lab-workflow/",
                axum::routing::get(crate::lab_api::lab_workflow),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/agent-plugins/versions/",
                axum::routing::get(crate::plugin_api::list_plugin_versions),
            )
            .route(
                "/api/v1/agent-plugins/{plugin_id}/activate/",
                axum::routing::post(crate::plugin_api::activate_plugin_version),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/fleet/machines/",
                axum::routing::get(crate::agent_runtime_api::fleet_machines),
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
                "/api/v1/cloud-vms/{vm_id}/",
                axum::routing::get(crate::cloud_vm_api::get_instance),
            )
            .route(
                "/api/v1/cloud-vms/{vm_id}/actions/",
                axum::routing::post(crate::cloud_vm_api::power_action),
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
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-plugins/{plugin_id}/capabilities/{capability}/invoke/",
                axum::routing::post(crate::plugin_api::invoke_project_capability),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-capabilities/",
                axum::routing::get(crate::plugin_api::list_capabilities),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/mcp/",
                axum::routing::post(crate::mcp_api::serve),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/events/stream/",
                axum::routing::get(crate::realtime_api::events_stream),
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
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/fleet/machines/discovery/",
                axum::routing::post(crate::fleet_boot::discovery),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/fleet/machines/pxe-boot/",
                axum::routing::post(crate::fleet_boot::pxe_boot),
            )
            .route(
                "/api/v1/tenants/{tenant_id}/projects/{project_id}/fleet/machines/bmh-inventory/",
                axum::routing::post(crate::fleet_api::bmh_inventory),
            )
            .route(
                "/api/v1/agent-runtime/inventory/",
                axum::routing::post(crate::agent_runtime_api::report_inventory),
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
                "/api/v1/agent-plugin-installations/{installation_id}/credential/",
                post(crate::plugin_api::rotate_installation_credential),
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
    router.fallback(not_found).with_state(state)
}

async fn not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({
            "error": {
                "code": "ROUTE_NOT_IMPLEMENTED",
                "message": "This endpoint is not implemented by the Rust API.",
            }
        })),
    )
        .into_response()
}

async fn edge_liveness() -> Response {
    Json(json!({
        "status": "ok",
        "service": "rust-api",
    }))
    .into_response()
}

async fn health() -> Response {
    Json(json!({
        "status": "ready",
        "service": "rust-api",
    }))
    .into_response()
}

#[derive(serde::Serialize)]
struct ApiInfo {
    service: &'static str,
    version: &'static str,
    websocket: bool,
    server_sent_events: bool,
}

async fn info() -> Json<ApiInfo> {
    Json(ApiInfo {
        service: "rust-api",
        version: env!("CARGO_PKG_VERSION"),
        websocket: false,
        server_sent_events: true,
    })
}
