-- Baseline business schema for a fresh database. Generated from the retired
-- retired platform migrations and normalized for PostgreSQL and SQLite.
-- 0001_initial
--
-- Create model Tenant
--
CREATE TABLE "core_tenant" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "slug" varchar(80) NOT NULL UNIQUE, "created_at" TIMESTAMP NOT NULL);
--
-- Create model Project
--
CREATE TABLE "core_project" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "slug" varchar(80) NOT NULL, "created_at" TIMESTAMP NOT NULL, "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_project_slug_per_tenant" UNIQUE ("tenant_id", "slug"));
--
-- Create model TenantMembership
--
CREATE TABLE "core_tenantmembership" ("id" TEXT NOT NULL PRIMARY KEY, "role" varchar(16) NOT NULL, "created_at" TIMESTAMP NOT NULL, "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "user_id" integer NOT NULL REFERENCES "platform_users" ("id"), CONSTRAINT "uniq_tenant_user_membership" UNIQUE ("tenant_id", "user_id"));
--
-- Create model ProjectMembership
--
CREATE TABLE "core_projectmembership" ("id" TEXT NOT NULL PRIMARY KEY, "role" varchar(16) NOT NULL, "created_at" TIMESTAMP NOT NULL, "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "user_id" integer NOT NULL REFERENCES "platform_users" ("id"), CONSTRAINT "uniq_project_user_membership" UNIQUE ("project_id", "user_id"));
CREATE INDEX "core_project_slug_5b8ba97e" ON "core_project" ("slug");
CREATE INDEX "core_project_tenant_id_560cac1a" ON "core_project" ("tenant_id");
CREATE INDEX "core_tenantmembership_tenant_id_dcdb258b" ON "core_tenantmembership" ("tenant_id");
CREATE INDEX "core_tenantmembership_user_id_6558adee" ON "core_tenantmembership" ("user_id");
CREATE INDEX "core_projectmembership_project_id_23908da6" ON "core_projectmembership" ("project_id");
CREATE INDEX "core_projectmembership_user_id_4a73ca06" ON "core_projectmembership" ("user_id");
-- 0002_task
--
-- Create model Task
--
CREATE TABLE "core_task" ("id" TEXT NOT NULL PRIMARY KEY, "task_type" varchar(120) NOT NULL, "payload" text NOT NULL CHECK ((JSON_VALID("payload") OR "payload" IS NULL)), "result" text NULL CHECK ((JSON_VALID("result") OR "result" IS NULL)), "error" text NULL CHECK ((JSON_VALID("error") OR "error" IS NULL)), "status" varchar(24) NOT NULL, "idempotency_key" varchar(128) NOT NULL, "attempts" smallint unsigned NOT NULL CHECK ("attempts" >= 0), "max_attempts" smallint unsigned NOT NULL CHECK ("max_attempts" >= 0), "available_at" TIMESTAMP NOT NULL, "lease_owner" varchar(120) NULL, "lease_token" TEXT NULL, "lease_expires_at" TIMESTAMP NULL, "heartbeat_at" TIMESTAMP NULL, "cancel_requested_at" TIMESTAMP NULL, "started_at" TIMESTAMP NULL, "finished_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_task_idempotency_per_tenant" UNIQUE ("tenant_id", "idempotency_key"));
CREATE INDEX "core_task_created_by_id_90325d11" ON "core_task" ("created_by_id");
CREATE INDEX "core_task_project_id_877ce78a" ON "core_task" ("project_id");
CREATE INDEX "core_task_tenant_id_e9784b4c" ON "core_task" ("tenant_id");
CREATE INDEX "task_queue_idx" ON "core_task" ("status", "available_at", "created_at");
CREATE INDEX "task_scope_idx" ON "core_task" ("tenant_id", "project_id", "created_at");
-- 0003_model_provider
--
-- Create model ModelProvider
--
CREATE TABLE "core_modelprovider" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "provider_type" varchar(32) NOT NULL, "base_url" varchar(500) NOT NULL, "credential_ref" varchar(160) NOT NULL, "config" text NOT NULL CHECK ((JSON_VALID("config") OR "config" IS NULL)), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_model_provider_name_per_tenant" UNIQUE ("tenant_id", "name"));
--
-- Create model ModelConfig
--
CREATE TABLE "core_modelconfig" ("id" TEXT NOT NULL PRIMARY KEY, "model_name" varchar(160) NOT NULL, "display_name" varchar(160) NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "context_window" integer unsigned NOT NULL CHECK ("context_window" >= 0), "max_output_tokens" integer unsigned NOT NULL CHECK ("max_output_tokens" >= 0), "input_cost_per_million" decimal NOT NULL, "output_cost_per_million" decimal NOT NULL, "version" integer unsigned NOT NULL CHECK ("version" >= 0), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "provider_id" TEXT NOT NULL REFERENCES "core_modelprovider" ("id"), CONSTRAINT "uniq_model_config_version" UNIQUE ("provider_id", "model_name", "version"));
CREATE INDEX "core_modelprovider_created_by_id_d65969a2" ON "core_modelprovider" ("created_by_id");
CREATE INDEX "core_modelprovider_tenant_id_7abf3c0d" ON "core_modelprovider" ("tenant_id");
CREATE INDEX "core_modelconfig_provider_id_bb8437e8" ON "core_modelconfig" ("provider_id");
-- 0004_agent
--
-- Create model AgentDefinition
--
CREATE TABLE "core_agentdefinition" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "slug" varchar(80) NOT NULL, "description" text NOT NULL, "draft_config" text NOT NULL CHECK ((JSON_VALID("draft_config") OR "draft_config" IS NULL)), "current_version" integer unsigned NOT NULL CHECK ("current_version" >= 0), "is_archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_slug_per_project" UNIQUE ("project_id", "slug"));
--
-- Create model AgentVersion
--
CREATE TABLE "core_agentversion" ("id" TEXT NOT NULL PRIMARY KEY, "version" integer unsigned NOT NULL CHECK ("version" >= 0), "config_snapshot" text NOT NULL CHECK ((JSON_VALID("config_snapshot") OR "config_snapshot" IS NULL)), "published_at" TIMESTAMP NOT NULL, "agent_id" TEXT NOT NULL REFERENCES "core_agentdefinition" ("id"), "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), CONSTRAINT "uniq_agent_version_number" UNIQUE ("agent_id", "version"));
--
-- Create model AgentRun
--
CREATE TABLE "core_agentrun" ("id" TEXT NOT NULL PRIMARY KEY, "input_text" text NOT NULL, "output_text" text NOT NULL, "input_tokens" integer unsigned NOT NULL CHECK ("input_tokens" >= 0), "output_tokens" integer unsigned NOT NULL CHECK ("output_tokens" >= 0), "provider_name" varchar(120) NOT NULL, "model_name" varchar(160) NOT NULL, "latency_ms" integer unsigned NOT NULL CHECK ("latency_ms" >= 0), "created_at" TIMESTAMP NOT NULL, "finished_at" TIMESTAMP NULL, "agent_version_id" TEXT NOT NULL REFERENCES "core_agentversion" ("id"), "task_id" TEXT NOT NULL UNIQUE REFERENCES "core_task" ("id"));
CREATE INDEX "core_agentdefinition_slug_83939021" ON "core_agentdefinition" ("slug");
CREATE INDEX "core_agentdefinition_created_by_id_04c0cc74" ON "core_agentdefinition" ("created_by_id");
CREATE INDEX "core_agentdefinition_project_id_a42704b7" ON "core_agentdefinition" ("project_id");
CREATE INDEX "core_agentdefinition_tenant_id_0a1221d3" ON "core_agentdefinition" ("tenant_id");
CREATE INDEX "core_agentversion_agent_id_7530df09" ON "core_agentversion" ("agent_id");
CREATE INDEX "core_agentversion_created_by_id_d3413cca" ON "core_agentversion" ("created_by_id");
CREATE INDEX "core_agentrun_agent_version_id_60e89770" ON "core_agentrun" ("agent_version_id");
-- 0005_workflow
--
-- Create model WorkflowDefinition
--
CREATE TABLE "core_workflowdefinition" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "slug" varchar(80) NOT NULL, "description" text NOT NULL, "draft_definition" text NOT NULL CHECK ((JSON_VALID("draft_definition") OR "draft_definition" IS NULL)), "current_version" integer unsigned NOT NULL CHECK ("current_version" >= 0), "is_archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_workflow_slug_per_project" UNIQUE ("project_id", "slug"));
--
-- Create model WorkflowVersion
--
CREATE TABLE "core_workflowversion" ("id" TEXT NOT NULL PRIMARY KEY, "version" integer unsigned NOT NULL CHECK ("version" >= 0), "definition_snapshot" text NOT NULL CHECK ((JSON_VALID("definition_snapshot") OR "definition_snapshot" IS NULL)), "published_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "workflow_id" TEXT NOT NULL REFERENCES "core_workflowdefinition" ("id"), CONSTRAINT "uniq_workflow_version_number" UNIQUE ("workflow_id", "version"));
--
-- Create model WorkflowRun
--
CREATE TABLE "core_workflowrun" ("id" TEXT NOT NULL PRIMARY KEY, "input_data" text NOT NULL CHECK ((JSON_VALID("input_data") OR "input_data" IS NULL)), "state" text NOT NULL CHECK ((JSON_VALID("state") OR "state" IS NULL)), "output_data" text NULL CHECK ((JSON_VALID("output_data") OR "output_data" IS NULL)), "created_at" TIMESTAMP NOT NULL, "finished_at" TIMESTAMP NULL, "task_id" TEXT NOT NULL UNIQUE REFERENCES "core_task" ("id"), "workflow_version_id" TEXT NOT NULL REFERENCES "core_workflowversion" ("id"));
--
-- Create model WorkflowStepRun
--
CREATE TABLE "core_workflowsteprun" ("id" TEXT NOT NULL PRIMARY KEY, "node_id" varchar(120) NOT NULL, "node_type" varchar(32) NOT NULL, "status" varchar(16) NOT NULL, "input_data" text NOT NULL CHECK ((JSON_VALID("input_data") OR "input_data" IS NULL)), "output_data" text NULL CHECK ((JSON_VALID("output_data") OR "output_data" IS NULL)), "error" text NULL CHECK ((JSON_VALID("error") OR "error" IS NULL)), "attempt" smallint unsigned NOT NULL CHECK ("attempt" >= 0), "started_at" TIMESTAMP NULL, "finished_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "workflow_run_id" TEXT NOT NULL REFERENCES "core_workflowrun" ("id"), CONSTRAINT "uniq_workflow_step_run_node" UNIQUE ("workflow_run_id", "node_id"));
CREATE INDEX "core_workflowdefinition_slug_79863184" ON "core_workflowdefinition" ("slug");
CREATE INDEX "core_workflowdefinition_created_by_id_f7ec5ae5" ON "core_workflowdefinition" ("created_by_id");
CREATE INDEX "core_workflowdefinition_project_id_3ca194db" ON "core_workflowdefinition" ("project_id");
CREATE INDEX "core_workflowdefinition_tenant_id_8da2bf0f" ON "core_workflowdefinition" ("tenant_id");
CREATE INDEX "core_workflowversion_created_by_id_69b98ee9" ON "core_workflowversion" ("created_by_id");
CREATE INDEX "core_workflowversion_workflow_id_a177a067" ON "core_workflowversion" ("workflow_id");
CREATE INDEX "core_workflowrun_workflow_version_id_80ef0f71" ON "core_workflowrun" ("workflow_version_id");
CREATE INDEX "core_workflowsteprun_workflow_run_id_b0f4b856" ON "core_workflowsteprun" ("workflow_run_id");
-- 0006_agent_nodes
--
-- Create model AgentNode
--
CREATE TABLE "core_agentnode" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "node_type" varchar(24) NOT NULL, "cluster_name" varchar(120) NOT NULL, "agent_version" varchar(40) NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "labels" text NOT NULL CHECK ((JSON_VALID("labels") OR "labels" IS NULL)), "credential_hash" varchar(64) NOT NULL, "status" varchar(16) NOT NULL, "last_seen_at" TIMESTAMP NULL, "last_seen_ip" TEXT NULL, "revoked_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_node_name_per_project" UNIQUE ("project_id", "name"));
--
-- Create model AgentCommand
--
CREATE TABLE "core_agentcommand" ("id" TEXT NOT NULL PRIMARY KEY, "command_type" varchar(64) NOT NULL, "payload" text NOT NULL CHECK ((JSON_VALID("payload") OR "payload" IS NULL)), "status" varchar(24) NOT NULL, "idempotency_key" varchar(128) NOT NULL, "plan_hash" varchar(64) NOT NULL, "approved_plan_hash" varchar(64) NOT NULL, "approved_at" TIMESTAMP NULL, "attempts" smallint unsigned NOT NULL CHECK ("attempts" >= 0), "max_attempts" smallint unsigned NOT NULL CHECK ("max_attempts" >= 0), "lease_token" TEXT NULL, "lease_expires_at" TIMESTAMP NULL, "result" text NULL CHECK ((JSON_VALID("result") OR "result" IS NULL)), "error" text NULL CHECK ((JSON_VALID("error") OR "error" IS NULL)), "started_at" TIMESTAMP NULL, "finished_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "approved_by_id" integer NULL REFERENCES "platform_users" ("id"), "node_id" TEXT NOT NULL REFERENCES "core_agentnode" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "requested_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_command_idempotency" UNIQUE ("tenant_id", "idempotency_key"));
CREATE INDEX "core_agentnode_created_by_id_9f5cd2cd" ON "core_agentnode" ("created_by_id");
CREATE INDEX "core_agentnode_project_id_8ac220c9" ON "core_agentnode" ("project_id");
CREATE INDEX "core_agentnode_tenant_id_89b7b63b" ON "core_agentnode" ("tenant_id");
CREATE INDEX "agent_node_scope_idx" ON "core_agentnode" ("tenant_id", "project_id", "node_type");
CREATE INDEX "agent_node_status_idx" ON "core_agentnode" ("status", "last_seen_at");
CREATE INDEX "core_agentcommand_approved_by_id_72da75c6" ON "core_agentcommand" ("approved_by_id");
CREATE INDEX "core_agentcommand_node_id_747c4561" ON "core_agentcommand" ("node_id");
CREATE INDEX "core_agentcommand_project_id_b9dbbb38" ON "core_agentcommand" ("project_id");
CREATE INDEX "core_agentcommand_requested_by_id_60e1a74d" ON "core_agentcommand" ("requested_by_id");
CREATE INDEX "core_agentcommand_tenant_id_6f246a81" ON "core_agentcommand" ("tenant_id");
CREATE INDEX "agent_command_queue_idx" ON "core_agentcommand" ("node_id", "status", "created_at");
CREATE INDEX "agent_command_scope_idx" ON "core_agentcommand" ("tenant_id", "project_id", "created_at");
-- 0007_cluster_deployment
--
-- Create model ClusterDeployment
--
CREATE TABLE "core_clusterdeployment" ("id" TEXT NOT NULL PRIMARY KEY, "cluster_name" varchar(63) NOT NULL, "kubernetes_version" varchar(32) NOT NULL, "spec" text NOT NULL CHECK ((JSON_VALID("spec") OR "spec" IS NULL)), "status" varchar(32) NOT NULL, "idempotency_key" varchar(128) NOT NULL, "plan_hash" varchar(64) NOT NULL, "preflight_result" text NULL CHECK ((JSON_VALID("preflight_result") OR "preflight_result" IS NULL)), "plan_result" text NULL CHECK ((JSON_VALID("plan_result") OR "plan_result" IS NULL)), "error" text NULL CHECK ((JSON_VALID("error") OR "error" IS NULL)), "started_at" TIMESTAMP NULL, "finished_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "eph_node_id" TEXT NOT NULL REFERENCES "core_agentnode" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "requested_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "target_node_id" TEXT NULL REFERENCES "core_agentnode" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_cluster_deployment_idempotency" UNIQUE ("tenant_id", "idempotency_key"));
--
-- Add field deployment to agentcommand
--
ALTER TABLE "core_agentcommand" ADD COLUMN "deployment_id" TEXT NULL REFERENCES "core_clusterdeployment" ("id");
CREATE INDEX "core_clusterdeployment_cluster_name_9c656236" ON "core_clusterdeployment" ("cluster_name");
CREATE INDEX "core_clusterdeployment_eph_node_id_f5f5bb76" ON "core_clusterdeployment" ("eph_node_id");
CREATE INDEX "core_clusterdeployment_project_id_2785ed4e" ON "core_clusterdeployment" ("project_id");
CREATE INDEX "core_clusterdeployment_requested_by_id_c7e02901" ON "core_clusterdeployment" ("requested_by_id");
CREATE INDEX "core_clusterdeployment_target_node_id_d393ce75" ON "core_clusterdeployment" ("target_node_id");
CREATE INDEX "core_clusterdeployment_tenant_id_6dc875ae" ON "core_clusterdeployment" ("tenant_id");
CREATE INDEX "cluster_deploy_scope_idx" ON "core_clusterdeployment" ("tenant_id", "project_id", "status");
CREATE INDEX "cluster_deploy_node_idx" ON "core_clusterdeployment" ("eph_node_id", "status", "created_at");
CREATE INDEX "core_agentcommand_deployment_id_3a3ec9ef" ON "core_agentcommand" ("deployment_id");
-- 0008_plugin_registry
--
-- Create model PluginDefinition
--
CREATE TABLE "core_plugindefinition" ("id" TEXT NOT NULL PRIMARY KEY, "plugin_key" varchar(100) NOT NULL, "name" varchar(120) NOT NULL, "description" text NOT NULL, "version" varchar(40) NOT NULL, "connector_type" varchar(16) NOT NULL, "maintainer" varchar(120) NOT NULL, "license" varchar(80) NOT NULL, "source_url" varchar(500) NOT NULL, "manifest" text NOT NULL CHECK ((JSON_VALID("manifest") OR "manifest" IS NULL)), "review_status" varchar(16) NOT NULL, "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_plugin_key_version_per_tenant" UNIQUE ("tenant_id", "plugin_key", "version"));
--
-- Create model PluginTool
--
CREATE TABLE "core_plugintool" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(100) NOT NULL, "description" text NOT NULL, "input_schema" text NOT NULL CHECK ((JSON_VALID("input_schema") OR "input_schema" IS NULL)), "output_schema" text NOT NULL CHECK ((JSON_VALID("output_schema") OR "output_schema" IS NULL)), "required_permissions" text NOT NULL CHECK ((JSON_VALID("required_permissions") OR "required_permissions" IS NULL)), "risk_level" varchar(16) NOT NULL, "timeout_seconds" smallint unsigned NOT NULL CHECK ("timeout_seconds" >= 0), "retry_policy" text NOT NULL CHECK ((JSON_VALID("retry_policy") OR "retry_policy" IS NULL)), "idempotency_mode" varchar(16) NOT NULL, "enabled" bool NOT NULL, "plugin_id" TEXT NOT NULL REFERENCES "core_plugindefinition" ("id"), CONSTRAINT "uniq_plugin_tool_name" UNIQUE ("plugin_id", "name"));
--
-- Create model PluginInstallation
--
CREATE TABLE "core_plugininstallation" ("id" TEXT NOT NULL PRIMARY KEY, "plugin_version" varchar(40) NOT NULL, "status" varchar(16) NOT NULL, "granted_permissions" text NOT NULL CHECK ((JSON_VALID("granted_permissions") OR "granted_permissions" IS NULL)), "config" text NOT NULL CHECK ((JSON_VALID("config") OR "config" IS NULL)), "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "installed_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "plugin_id" TEXT NOT NULL REFERENCES "core_plugindefinition" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_plugin_installation_per_project" UNIQUE ("project_id", "plugin_id"));
--
-- Create model PluginConnection
--
CREATE TABLE "core_pluginconnection" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "auth_type" varchar(20) NOT NULL, "credential_ref" varchar(200) NOT NULL, "config" text NOT NULL CHECK ((JSON_VALID("config") OR "config" IS NULL)), "status" varchar(16) NOT NULL, "last_health_check_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "installation_id" TEXT NOT NULL REFERENCES "core_plugininstallation" ("id"), CONSTRAINT "uniq_plugin_connection_name" UNIQUE ("installation_id", "name"));
CREATE INDEX "core_plugindefinition_plugin_key_015769dd" ON "core_plugindefinition" ("plugin_key");
CREATE INDEX "core_plugindefinition_created_by_id_824220ab" ON "core_plugindefinition" ("created_by_id");
CREATE INDEX "core_plugindefinition_tenant_id_6963cd4f" ON "core_plugindefinition" ("tenant_id");
CREATE INDEX "core_plugintool_name_b8b155a9" ON "core_plugintool" ("name");
CREATE INDEX "core_plugintool_plugin_id_d34d7fbf" ON "core_plugintool" ("plugin_id");
CREATE INDEX "core_plugininstallation_installed_by_id_3c691b0c" ON "core_plugininstallation" ("installed_by_id");
CREATE INDEX "core_plugininstallation_plugin_id_67f08f37" ON "core_plugininstallation" ("plugin_id");
CREATE INDEX "core_plugininstallation_project_id_1dfbf91e" ON "core_plugininstallation" ("project_id");
CREATE INDEX "core_plugininstallation_tenant_id_1040c76f" ON "core_plugininstallation" ("tenant_id");
CREATE INDEX "core_pluginconnection_installation_id_c2437a44" ON "core_pluginconnection" ("installation_id");
-- 0009_knowledge_base
--
-- Create model KnowledgeBase
--
CREATE TABLE "core_knowledgebase" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "slug" varchar(80) NOT NULL, "description" text NOT NULL, "chunk_size" smallint unsigned NOT NULL CHECK ("chunk_size" >= 0), "chunk_overlap" smallint unsigned NOT NULL CHECK ("chunk_overlap" >= 0), "is_archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "embedding_model_id" TEXT NOT NULL REFERENCES "core_modelconfig" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_knowledge_base_slug_per_project" UNIQUE ("project_id", "slug"));
--
-- Create model KnowledgeDocument
--
CREATE TABLE "core_knowledgedocument" ("id" TEXT NOT NULL PRIMARY KEY, "title" varchar(240) NOT NULL, "content" text NOT NULL, "metadata" text NOT NULL CHECK ((JSON_VALID("metadata") OR "metadata" IS NULL)), "checksum" varchar(64) NOT NULL, "status" varchar(16) NOT NULL, "chunk_count" integer unsigned NOT NULL CHECK ("chunk_count" >= 0), "error" text NULL CHECK ((JSON_VALID("error") OR "error" IS NULL)), "ingested_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "knowledge_base_id" TEXT NOT NULL REFERENCES "core_knowledgebase" ("id"), "uploaded_by_id" integer NOT NULL REFERENCES "platform_users" ("id"));
--
-- Create index knowledge_doc_status_idx on field(s) knowledge_base, status, created_at of model knowledgedocument
--
CREATE INDEX "knowledge_doc_status_idx" ON "core_knowledgedocument" ("knowledge_base_id", "status", "created_at");
CREATE INDEX "core_knowledgebase_slug_787d1b62" ON "core_knowledgebase" ("slug");
CREATE INDEX "core_knowledgebase_created_by_id_43ed05e6" ON "core_knowledgebase" ("created_by_id");
CREATE INDEX "core_knowledgebase_embedding_model_id_595d9600" ON "core_knowledgebase" ("embedding_model_id");
CREATE INDEX "core_knowledgebase_project_id_e1e47c66" ON "core_knowledgebase" ("project_id");
CREATE INDEX "core_knowledgebase_tenant_id_58dba496" ON "core_knowledgebase" ("tenant_id");
CREATE INDEX "core_knowledgedocument_knowledge_base_id_9963f00c" ON "core_knowledgedocument" ("knowledge_base_id");
CREATE INDEX "core_knowledgedocument_uploaded_by_id_be7eb163" ON "core_knowledgedocument" ("uploaded_by_id");
-- 0010_deployment_checkpoint
--
-- Create model ClusterDeploymentCheckpoint
--
CREATE TABLE "core_clusterdeploymentcheckpoint" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "generation" integer unsigned NOT NULL CHECK ("generation" >= 0), "sequence" bigint unsigned NOT NULL CHECK ("sequence" >= 0), "phase" varchar(64) NOT NULL, "data" text NOT NULL CHECK ((JSON_VALID("data") OR "data" IS NULL)), "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_node_id" TEXT NOT NULL REFERENCES "core_agentnode" ("id"), "deployment_id" TEXT NOT NULL UNIQUE REFERENCES "core_clusterdeployment" ("id"));
CREATE INDEX "core_clusterdeploymentcheckpoint_agent_node_id_1de07b6d" ON "core_clusterdeploymentcheckpoint" ("agent_node_id");
-- 0011_audit_event
--
-- Create model AuditEvent
--
CREATE TABLE "core_auditevent" ("id" TEXT NOT NULL PRIMARY KEY, "event_type" varchar(120) NOT NULL, "resource_type" varchar(80) NOT NULL, "resource_id" varchar(64) NOT NULL, "outcome" varchar(16) NOT NULL, "request_id" varchar(64) NOT NULL, "remote_addr" TEXT NULL, "user_agent" varchar(256) NOT NULL, "metadata" text NOT NULL CHECK ((JSON_VALID("metadata") OR "metadata" IS NULL)), "previous_hash" varchar(64) NOT NULL, "event_hash" varchar(64) NOT NULL UNIQUE, "created_at" TIMESTAMP NOT NULL, "actor_id" integer NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create index audit_tenant_time_idx on field(s) tenant, -created_at of model auditevent
--
CREATE INDEX "audit_tenant_time_idx" ON "core_auditevent" ("tenant_id", "created_at" DESC);
--
-- Create index audit_tenant_type_idx on field(s) tenant, event_type, -created_at of model auditevent
--
CREATE INDEX "audit_tenant_type_idx" ON "core_auditevent" ("tenant_id", "event_type", "created_at" DESC);
CREATE INDEX "core_auditevent_actor_id_3bd0c673" ON "core_auditevent" ("actor_id");
CREATE INDEX "core_auditevent_project_id_f3f2c86f" ON "core_auditevent" ("project_id");
CREATE INDEX "core_auditevent_tenant_id_43ff9a90" ON "core_auditevent" ("tenant_id");
-- 0012_tool_approval_request
--
-- Create model ToolApprovalRequest
--
CREATE TABLE "core_toolapprovalrequest" ("id" TEXT NOT NULL PRIMARY KEY, "call_id" varchar(128) NOT NULL, "arguments" text NOT NULL CHECK ((JSON_VALID("arguments") OR "arguments" IS NULL)), "arguments_hash" varchar(64) NOT NULL, "plugin_version" varchar(40) NOT NULL, "status" varchar(16) NOT NULL, "requested_at" TIMESTAMP NOT NULL, "expires_at" TIMESTAMP NOT NULL, "approved_at" TIMESTAMP NULL, "consumed_at" TIMESTAMP NULL, "rejection_reason" varchar(500) NOT NULL, "approved_by_id" integer NULL REFERENCES "platform_users" ("id"), "connection_id" TEXT NULL REFERENCES "core_pluginconnection" ("id"), "installation_id" TEXT NOT NULL REFERENCES "core_plugininstallation" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "task_id" TEXT NOT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "tool_id" TEXT NOT NULL REFERENCES "core_plugintool" ("id"));
--
-- Create constraint uniq_tool_approval_task_call on model toolapprovalrequest
--
CREATE TABLE "new__core_toolapprovalrequest" ("id" TEXT NOT NULL PRIMARY KEY, "call_id" varchar(128) NOT NULL, "arguments" text NOT NULL CHECK ((JSON_VALID("arguments") OR "arguments" IS NULL)), "arguments_hash" varchar(64) NOT NULL, "plugin_version" varchar(40) NOT NULL, "status" varchar(16) NOT NULL, "requested_at" TIMESTAMP NOT NULL, "expires_at" TIMESTAMP NOT NULL, "approved_at" TIMESTAMP NULL, "consumed_at" TIMESTAMP NULL, "rejection_reason" varchar(500) NOT NULL, "approved_by_id" integer NULL REFERENCES "platform_users" ("id"), "connection_id" TEXT NULL REFERENCES "core_pluginconnection" ("id"), "installation_id" TEXT NOT NULL REFERENCES "core_plugininstallation" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "task_id" TEXT NOT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "tool_id" TEXT NOT NULL REFERENCES "core_plugintool" ("id"), CONSTRAINT "uniq_tool_approval_task_call" UNIQUE ("task_id", "call_id"));
INSERT INTO "new__core_toolapprovalrequest" ("id", "call_id", "arguments", "arguments_hash", "plugin_version", "status", "requested_at", "expires_at", "approved_at", "consumed_at", "rejection_reason", "approved_by_id", "connection_id", "installation_id", "project_id", "task_id", "tenant_id", "tool_id") SELECT "id", "call_id", "arguments", "arguments_hash", "plugin_version", "status", "requested_at", "expires_at", "approved_at", "consumed_at", "rejection_reason", "approved_by_id", "connection_id", "installation_id", "project_id", "task_id", "tenant_id", "tool_id" FROM "core_toolapprovalrequest";
DROP TABLE "core_toolapprovalrequest";
ALTER TABLE "new__core_toolapprovalrequest" RENAME TO "core_toolapprovalrequest";
CREATE INDEX "core_toolapprovalrequest_approved_by_id_88967a3d" ON "core_toolapprovalrequest" ("approved_by_id");
CREATE INDEX "core_toolapprovalrequest_connection_id_a625b69e" ON "core_toolapprovalrequest" ("connection_id");
CREATE INDEX "core_toolapprovalrequest_installation_id_6c661255" ON "core_toolapprovalrequest" ("installation_id");
CREATE INDEX "core_toolapprovalrequest_project_id_48f7b470" ON "core_toolapprovalrequest" ("project_id");
CREATE INDEX "core_toolapprovalrequest_task_id_3d923087" ON "core_toolapprovalrequest" ("task_id");
CREATE INDEX "core_toolapprovalrequest_tenant_id_fd288c9c" ON "core_toolapprovalrequest" ("tenant_id");
CREATE INDEX "core_toolapprovalrequest_tool_id_b88d78a5" ON "core_toolapprovalrequest" ("tool_id");
--
-- Create index tool_approval_scope_idx on field(s) tenant, project, status, expires_at of model toolapprovalrequest
--
CREATE INDEX "tool_approval_scope_idx" ON "core_toolapprovalrequest" ("tenant_id", "project_id", "status", "expires_at");
-- 0013_tool_approval_execution_checkpoint
--
-- Add field execution_checkpoint to toolapprovalrequest
--
CREATE TABLE "new__core_toolapprovalrequest" ("execution_checkpoint" text NOT NULL CHECK ((JSON_VALID("execution_checkpoint") OR "execution_checkpoint" IS NULL)), "id" TEXT NOT NULL PRIMARY KEY, "call_id" varchar(128) NOT NULL, "arguments" text NOT NULL CHECK ((JSON_VALID("arguments") OR "arguments" IS NULL)), "arguments_hash" varchar(64) NOT NULL, "plugin_version" varchar(40) NOT NULL, "status" varchar(16) NOT NULL, "requested_at" TIMESTAMP NOT NULL, "expires_at" TIMESTAMP NOT NULL, "approved_at" TIMESTAMP NULL, "consumed_at" TIMESTAMP NULL, "rejection_reason" varchar(500) NOT NULL, "approved_by_id" integer NULL REFERENCES "platform_users" ("id"), "connection_id" TEXT NULL REFERENCES "core_pluginconnection" ("id"), "installation_id" TEXT NOT NULL REFERENCES "core_plugininstallation" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "task_id" TEXT NOT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "tool_id" TEXT NOT NULL REFERENCES "core_plugintool" ("id"), CONSTRAINT "uniq_tool_approval_task_call" UNIQUE ("task_id", "call_id"));
INSERT INTO "new__core_toolapprovalrequest" ("id", "call_id", "arguments", "arguments_hash", "plugin_version", "status", "requested_at", "expires_at", "approved_at", "consumed_at", "rejection_reason", "approved_by_id", "connection_id", "installation_id", "project_id", "task_id", "tenant_id", "tool_id", "execution_checkpoint") SELECT "id", "call_id", "arguments", "arguments_hash", "plugin_version", "status", "requested_at", "expires_at", "approved_at", "consumed_at", "rejection_reason", "approved_by_id", "connection_id", "installation_id", "project_id", "task_id", "tenant_id", "tool_id", '{}' FROM "core_toolapprovalrequest";
DROP TABLE "core_toolapprovalrequest";
ALTER TABLE "new__core_toolapprovalrequest" RENAME TO "core_toolapprovalrequest";
CREATE INDEX "core_toolapprovalrequest_approved_by_id_88967a3d" ON "core_toolapprovalrequest" ("approved_by_id");
CREATE INDEX "core_toolapprovalrequest_connection_id_a625b69e" ON "core_toolapprovalrequest" ("connection_id");
CREATE INDEX "core_toolapprovalrequest_installation_id_6c661255" ON "core_toolapprovalrequest" ("installation_id");
CREATE INDEX "core_toolapprovalrequest_project_id_48f7b470" ON "core_toolapprovalrequest" ("project_id");
CREATE INDEX "core_toolapprovalrequest_task_id_3d923087" ON "core_toolapprovalrequest" ("task_id");
CREATE INDEX "core_toolapprovalrequest_tenant_id_fd288c9c" ON "core_toolapprovalrequest" ("tenant_id");
CREATE INDEX "core_toolapprovalrequest_tool_id_b88d78a5" ON "core_toolapprovalrequest" ("tool_id");
CREATE INDEX "tool_approval_scope_idx" ON "core_toolapprovalrequest" ("tenant_id", "project_id", "status", "expires_at");
-- 0014_tool_approval_rejection_audit
--
-- Add field rejected_by to toolapprovalrequest
--
ALTER TABLE "core_toolapprovalrequest" ADD COLUMN "rejected_by_id" integer NULL REFERENCES "platform_users" ("id");
--
-- Add field rejected_at to toolapprovalrequest
--
ALTER TABLE "core_toolapprovalrequest" ADD COLUMN "rejected_at" TIMESTAMP NULL;
CREATE INDEX "core_toolapprovalrequest_rejected_by_id_d989bb94" ON "core_toolapprovalrequest" ("rejected_by_id");
-- 0015_agent_machine_inventory
--
-- Create model AgentMachine
--
CREATE TABLE "core_agentmachine" ("id" TEXT NOT NULL PRIMARY KEY, "vendor" varchar(256) NOT NULL, "product_name" varchar(256) NOT NULL, "system_uuid" varchar(128) NOT NULL, "identity_material" text NOT NULL CHECK ((JSON_VALID("identity_material") OR "identity_material" IS NULL)), "identity_fingerprint" varchar(64) NOT NULL, "identity_status" varchar(16) NOT NULL, "identity_conflicts" text NOT NULL CHECK ((JSON_VALID("identity_conflicts") OR "identity_conflicts" IS NULL)), "hardware_inventory" text NOT NULL CHECK ((JSON_VALID("hardware_inventory") OR "hardware_inventory" IS NULL)), "inventory_source" varchar(48) NOT NULL, "observed_at" TIMESTAMP NULL, "bmc_credential_ref" varchar(160) NOT NULL, "bmh_name" varchar(253) NOT NULL, "capi_machine_name" varchar(253) NOT NULL, "kubernetes_node_name" varchar(253) NOT NULL, "lifecycle_state" varchar(32) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_node_id" TEXT NOT NULL UNIQUE REFERENCES "core_agentnode" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create index machine_identity_idx on field(s) tenant, project, identity_status of model agentmachine
--
CREATE INDEX "machine_identity_idx" ON "core_agentmachine" ("tenant_id", "project_id", "identity_status");
--
-- Create index machine_observed_idx on field(s) tenant, project, observed_at of model agentmachine
--
CREATE INDEX "machine_observed_idx" ON "core_agentmachine" ("tenant_id", "project_id", "observed_at");
CREATE INDEX "core_agentmachine_project_id_22866b67" ON "core_agentmachine" ("project_id");
CREATE INDEX "core_agentmachine_tenant_id_f6be2e25" ON "core_agentmachine" ("tenant_id");
-- 0016_agent_machine_bmc_config
--
-- Add field bmc_endpoint to agentmachine
--
CREATE TABLE "new__core_agentmachine" ("bmc_endpoint" varchar(500) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "vendor" varchar(256) NOT NULL, "product_name" varchar(256) NOT NULL, "system_uuid" varchar(128) NOT NULL, "identity_material" text NOT NULL CHECK ((JSON_VALID("identity_material") OR "identity_material" IS NULL)), "identity_fingerprint" varchar(64) NOT NULL, "identity_status" varchar(16) NOT NULL, "identity_conflicts" text NOT NULL CHECK ((JSON_VALID("identity_conflicts") OR "identity_conflicts" IS NULL)), "hardware_inventory" text NOT NULL CHECK ((JSON_VALID("hardware_inventory") OR "hardware_inventory" IS NULL)), "inventory_source" varchar(48) NOT NULL, "observed_at" TIMESTAMP NULL, "bmc_credential_ref" varchar(160) NOT NULL, "bmh_name" varchar(253) NOT NULL, "capi_machine_name" varchar(253) NOT NULL, "kubernetes_node_name" varchar(253) NOT NULL, "lifecycle_state" varchar(32) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_node_id" TEXT NOT NULL UNIQUE REFERENCES "core_agentnode" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
INSERT INTO "new__core_agentmachine" ("id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint") SELECT "id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", '' FROM "core_agentmachine";
DROP TABLE "core_agentmachine";
ALTER TABLE "new__core_agentmachine" RENAME TO "core_agentmachine";
CREATE INDEX "core_agentmachine_project_id_22866b67" ON "core_agentmachine" ("project_id");
CREATE INDEX "core_agentmachine_tenant_id_f6be2e25" ON "core_agentmachine" ("tenant_id");
CREATE INDEX "machine_identity_idx" ON "core_agentmachine" ("tenant_id", "project_id", "identity_status");
CREATE INDEX "machine_observed_idx" ON "core_agentmachine" ("tenant_id", "project_id", "observed_at");
--
-- Add field bmc_trust_bundle_ref to agentmachine
--
CREATE TABLE "new__core_agentmachine" ("id" TEXT NOT NULL PRIMARY KEY, "vendor" varchar(256) NOT NULL, "product_name" varchar(256) NOT NULL, "system_uuid" varchar(128) NOT NULL, "identity_material" text NOT NULL CHECK ((JSON_VALID("identity_material") OR "identity_material" IS NULL)), "identity_fingerprint" varchar(64) NOT NULL, "identity_status" varchar(16) NOT NULL, "identity_conflicts" text NOT NULL CHECK ((JSON_VALID("identity_conflicts") OR "identity_conflicts" IS NULL)), "hardware_inventory" text NOT NULL CHECK ((JSON_VALID("hardware_inventory") OR "hardware_inventory" IS NULL)), "inventory_source" varchar(48) NOT NULL, "observed_at" TIMESTAMP NULL, "bmc_credential_ref" varchar(160) NOT NULL, "bmh_name" varchar(253) NOT NULL, "capi_machine_name" varchar(253) NOT NULL, "kubernetes_node_name" varchar(253) NOT NULL, "lifecycle_state" varchar(32) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_node_id" TEXT NOT NULL UNIQUE REFERENCES "core_agentnode" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "bmc_endpoint" varchar(500) NOT NULL, "bmc_trust_bundle_ref" varchar(160) NOT NULL);
INSERT INTO "new__core_agentmachine" ("id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint", "bmc_trust_bundle_ref") SELECT "id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint", '' FROM "core_agentmachine";
DROP TABLE "core_agentmachine";
ALTER TABLE "new__core_agentmachine" RENAME TO "core_agentmachine";
CREATE INDEX "core_agentmachine_project_id_22866b67" ON "core_agentmachine" ("project_id");
CREATE INDEX "core_agentmachine_tenant_id_f6be2e25" ON "core_agentmachine" ("tenant_id");
CREATE INDEX "machine_identity_idx" ON "core_agentmachine" ("tenant_id", "project_id", "identity_status");
CREATE INDEX "machine_observed_idx" ON "core_agentmachine" ("tenant_id", "project_id", "observed_at");
--
-- Add field bmc_tls_verify to agentmachine
--
CREATE TABLE "new__core_agentmachine" ("id" TEXT NOT NULL PRIMARY KEY, "vendor" varchar(256) NOT NULL, "product_name" varchar(256) NOT NULL, "system_uuid" varchar(128) NOT NULL, "identity_material" text NOT NULL CHECK ((JSON_VALID("identity_material") OR "identity_material" IS NULL)), "identity_fingerprint" varchar(64) NOT NULL, "identity_status" varchar(16) NOT NULL, "identity_conflicts" text NOT NULL CHECK ((JSON_VALID("identity_conflicts") OR "identity_conflicts" IS NULL)), "hardware_inventory" text NOT NULL CHECK ((JSON_VALID("hardware_inventory") OR "hardware_inventory" IS NULL)), "inventory_source" varchar(48) NOT NULL, "observed_at" TIMESTAMP NULL, "bmc_credential_ref" varchar(160) NOT NULL, "bmh_name" varchar(253) NOT NULL, "capi_machine_name" varchar(253) NOT NULL, "kubernetes_node_name" varchar(253) NOT NULL, "lifecycle_state" varchar(32) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_node_id" TEXT NOT NULL UNIQUE REFERENCES "core_agentnode" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "bmc_endpoint" varchar(500) NOT NULL, "bmc_trust_bundle_ref" varchar(160) NOT NULL, "bmc_tls_verify" bool NOT NULL);
INSERT INTO "new__core_agentmachine" ("id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint", "bmc_trust_bundle_ref", "bmc_tls_verify") SELECT "id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint", "bmc_trust_bundle_ref", 1 FROM "core_agentmachine";
DROP TABLE "core_agentmachine";
ALTER TABLE "new__core_agentmachine" RENAME TO "core_agentmachine";
CREATE INDEX "core_agentmachine_project_id_22866b67" ON "core_agentmachine" ("project_id");
CREATE INDEX "core_agentmachine_tenant_id_f6be2e25" ON "core_agentmachine" ("tenant_id");
CREATE INDEX "machine_identity_idx" ON "core_agentmachine" ("tenant_id", "project_id", "identity_status");
CREATE INDEX "machine_observed_idx" ON "core_agentmachine" ("tenant_id", "project_id", "observed_at");
--
-- Add field bmc_configured_at to agentmachine
--
ALTER TABLE "core_agentmachine" ADD COLUMN "bmc_configured_at" TIMESTAMP NULL;
--
-- Add field bmc_identity_evidence to agentmachine
--
CREATE TABLE "new__core_agentmachine" ("id" TEXT NOT NULL PRIMARY KEY, "vendor" varchar(256) NOT NULL, "product_name" varchar(256) NOT NULL, "system_uuid" varchar(128) NOT NULL, "identity_material" text NOT NULL CHECK ((JSON_VALID("identity_material") OR "identity_material" IS NULL)), "identity_fingerprint" varchar(64) NOT NULL, "identity_status" varchar(16) NOT NULL, "identity_conflicts" text NOT NULL CHECK ((JSON_VALID("identity_conflicts") OR "identity_conflicts" IS NULL)), "hardware_inventory" text NOT NULL CHECK ((JSON_VALID("hardware_inventory") OR "hardware_inventory" IS NULL)), "inventory_source" varchar(48) NOT NULL, "observed_at" TIMESTAMP NULL, "bmc_credential_ref" varchar(160) NOT NULL, "bmh_name" varchar(253) NOT NULL, "capi_machine_name" varchar(253) NOT NULL, "kubernetes_node_name" varchar(253) NOT NULL, "lifecycle_state" varchar(32) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_node_id" TEXT NOT NULL UNIQUE REFERENCES "core_agentnode" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "bmc_endpoint" varchar(500) NOT NULL, "bmc_trust_bundle_ref" varchar(160) NOT NULL, "bmc_tls_verify" bool NOT NULL, "bmc_configured_at" TIMESTAMP NULL, "bmc_identity_evidence" text NOT NULL CHECK ((JSON_VALID("bmc_identity_evidence") OR "bmc_identity_evidence" IS NULL)));
INSERT INTO "new__core_agentmachine" ("id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint", "bmc_trust_bundle_ref", "bmc_tls_verify", "bmc_configured_at", "bmc_identity_evidence") SELECT "id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint", "bmc_trust_bundle_ref", "bmc_tls_verify", "bmc_configured_at", '{}' FROM "core_agentmachine";
DROP TABLE "core_agentmachine";
ALTER TABLE "new__core_agentmachine" RENAME TO "core_agentmachine";
CREATE INDEX "core_agentmachine_project_id_22866b67" ON "core_agentmachine" ("project_id");
CREATE INDEX "core_agentmachine_tenant_id_f6be2e25" ON "core_agentmachine" ("tenant_id");
CREATE INDEX "machine_identity_idx" ON "core_agentmachine" ("tenant_id", "project_id", "identity_status");
CREATE INDEX "machine_observed_idx" ON "core_agentmachine" ("tenant_id", "project_id", "observed_at");
-- 0017_agent_node_client_certificate
--
-- Add field client_certificate_fingerprint to agentnode
--
CREATE TABLE "new__core_agentnode" ("client_certificate_fingerprint" varchar(64) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "node_type" varchar(24) NOT NULL, "cluster_name" varchar(120) NOT NULL, "agent_version" varchar(40) NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "labels" text NOT NULL CHECK ((JSON_VALID("labels") OR "labels" IS NULL)), "credential_hash" varchar(64) NOT NULL, "status" varchar(16) NOT NULL, "last_seen_at" TIMESTAMP NULL, "last_seen_ip" TEXT NULL, "revoked_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_node_name_per_project" UNIQUE ("project_id", "name"));
INSERT INTO "new__core_agentnode" ("id", "name", "node_type", "cluster_name", "agent_version", "capabilities", "labels", "credential_hash", "status", "last_seen_at", "last_seen_ip", "revoked_at", "created_at", "updated_at", "created_by_id", "project_id", "tenant_id", "client_certificate_fingerprint") SELECT "id", "name", "node_type", "cluster_name", "agent_version", "capabilities", "labels", "credential_hash", "status", "last_seen_at", "last_seen_ip", "revoked_at", "created_at", "updated_at", "created_by_id", "project_id", "tenant_id", '' FROM "core_agentnode";
DROP TABLE "core_agentnode";
ALTER TABLE "new__core_agentnode" RENAME TO "core_agentnode";
CREATE INDEX "core_agentnode_created_by_id_9f5cd2cd" ON "core_agentnode" ("created_by_id");
CREATE INDEX "core_agentnode_project_id_8ac220c9" ON "core_agentnode" ("project_id");
CREATE INDEX "core_agentnode_tenant_id_89b7b63b" ON "core_agentnode" ("tenant_id");
CREATE INDEX "agent_node_scope_idx" ON "core_agentnode" ("tenant_id", "project_id", "node_type");
CREATE INDEX "agent_node_status_idx" ON "core_agentnode" ("status", "last_seen_at");
--
-- Add field client_certificate_bound_at to agentnode
--
ALTER TABLE "core_agentnode" ADD COLUMN "client_certificate_bound_at" TIMESTAMP NULL;
--
-- Add field client_certificate_revoked_at to agentnode
--
ALTER TABLE "core_agentnode" ADD COLUMN "client_certificate_revoked_at" TIMESTAMP NULL;
--
-- Create constraint uniq_agent_client_cert_fingerprint on model agentnode
--
CREATE UNIQUE INDEX "uniq_agent_client_cert_fingerprint" ON "core_agentnode" ("client_certificate_fingerprint") WHERE NOT ("client_certificate_fingerprint" = '');
-- 0018_agentrun_execution_state
--
-- Add field execution_state to agentrun
--
CREATE TABLE "new__core_agentrun" ("execution_state" text NOT NULL CHECK ((JSON_VALID("execution_state") OR "execution_state" IS NULL)), "id" TEXT NOT NULL PRIMARY KEY, "input_text" text NOT NULL, "output_text" text NOT NULL, "input_tokens" integer unsigned NOT NULL CHECK ("input_tokens" >= 0), "output_tokens" integer unsigned NOT NULL CHECK ("output_tokens" >= 0), "provider_name" varchar(120) NOT NULL, "model_name" varchar(160) NOT NULL, "latency_ms" integer unsigned NOT NULL CHECK ("latency_ms" >= 0), "created_at" TIMESTAMP NOT NULL, "finished_at" TIMESTAMP NULL, "agent_version_id" TEXT NOT NULL REFERENCES "core_agentversion" ("id"), "task_id" TEXT NOT NULL UNIQUE REFERENCES "core_task" ("id"));
INSERT INTO "new__core_agentrun" ("id", "input_text", "output_text", "input_tokens", "output_tokens", "provider_name", "model_name", "latency_ms", "created_at", "finished_at", "agent_version_id", "task_id", "execution_state") SELECT "id", "input_text", "output_text", "input_tokens", "output_tokens", "provider_name", "model_name", "latency_ms", "created_at", "finished_at", "agent_version_id", "task_id", '{}' FROM "core_agentrun";
DROP TABLE "core_agentrun";
ALTER TABLE "new__core_agentrun" RENAME TO "core_agentrun";
CREATE INDEX "core_agentrun_agent_version_id_60e89770" ON "core_agentrun" ("agent_version_id");
-- 0019_tenant_token_budget
--
-- Create model TenantTokenBudget
--
CREATE TABLE "core_tenanttokenbudget" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "daily_limit" bigint unsigned NOT NULL CHECK ("daily_limit" >= 0), "monthly_limit" bigint unsigned NOT NULL CHECK ("monthly_limit" >= 0), "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "tenant_id" TEXT NOT NULL UNIQUE REFERENCES "core_tenant" ("id"));
--
-- Create model TenantTokenUsage
--
CREATE TABLE "core_tenanttokenusage" ("id" TEXT NOT NULL PRIMARY KEY, "idempotency_key" varchar(160) NOT NULL, "status" varchar(16) NOT NULL, "reserved_tokens" bigint unsigned NOT NULL CHECK ("reserved_tokens" >= 0), "consumed_tokens" bigint unsigned NOT NULL CHECK ("consumed_tokens" >= 0), "expires_at" TIMESTAMP NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_run_id" TEXT NOT NULL REFERENCES "core_agentrun" ("id"), "task_id" TEXT NOT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create constraint uniq_tenant_token_usage_idempotency on model tenanttokenusage
--
CREATE TABLE "new__core_tenanttokenusage" ("id" TEXT NOT NULL PRIMARY KEY, "idempotency_key" varchar(160) NOT NULL, "status" varchar(16) NOT NULL, "reserved_tokens" bigint unsigned NOT NULL CHECK ("reserved_tokens" >= 0), "consumed_tokens" bigint unsigned NOT NULL CHECK ("consumed_tokens" >= 0), "expires_at" TIMESTAMP NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_run_id" TEXT NOT NULL REFERENCES "core_agentrun" ("id"), "task_id" TEXT NOT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_tenant_token_usage_idempotency" UNIQUE ("tenant_id", "idempotency_key"));
INSERT INTO "new__core_tenanttokenusage" ("id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "task_id", "tenant_id") SELECT "id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "task_id", "tenant_id" FROM "core_tenanttokenusage";
DROP TABLE "core_tenanttokenusage";
ALTER TABLE "new__core_tenanttokenusage" RENAME TO "core_tenanttokenusage";
CREATE INDEX "core_tenanttokenusage_agent_run_id_562c15dc" ON "core_tenanttokenusage" ("agent_run_id");
CREATE INDEX "core_tenanttokenusage_task_id_b948d773" ON "core_tenanttokenusage" ("task_id");
CREATE INDEX "core_tenanttokenusage_tenant_id_2d1f25b3" ON "core_tenanttokenusage" ("tenant_id");
--
-- Create index tenant_token_usage_period_idx on field(s) tenant, status, created_at of model tenanttokenusage
--
CREATE INDEX "tenant_token_usage_period_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "created_at");
--
-- Alter field agent_run on tenanttokenusage
--
CREATE TABLE "new__core_tenanttokenusage" ("id" TEXT NOT NULL PRIMARY KEY, "idempotency_key" varchar(160) NOT NULL, "status" varchar(16) NOT NULL, "reserved_tokens" bigint unsigned NOT NULL CHECK ("reserved_tokens" >= 0), "consumed_tokens" bigint unsigned NOT NULL CHECK ("consumed_tokens" >= 0), "expires_at" TIMESTAMP NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "task_id" TEXT NOT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "agent_run_id" TEXT NULL REFERENCES "core_agentrun" ("id"), CONSTRAINT "uniq_tenant_token_usage_idempotency" UNIQUE ("tenant_id", "idempotency_key"));
INSERT INTO "new__core_tenanttokenusage" ("id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "task_id", "tenant_id", "agent_run_id") SELECT "id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "task_id", "tenant_id", "agent_run_id" FROM "core_tenanttokenusage";
DROP TABLE "core_tenanttokenusage";
ALTER TABLE "new__core_tenanttokenusage" RENAME TO "core_tenanttokenusage";
CREATE INDEX "core_tenanttokenusage_task_id_b948d773" ON "core_tenanttokenusage" ("task_id");
CREATE INDEX "core_tenanttokenusage_tenant_id_2d1f25b3" ON "core_tenanttokenusage" ("tenant_id");
CREATE INDEX "core_tenanttokenusage_agent_run_id_562c15dc" ON "core_tenanttokenusage" ("agent_run_id");
CREATE INDEX "tenant_token_usage_period_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "created_at");
--
-- Add field workflow_run to tenanttokenusage
--
ALTER TABLE "core_tenanttokenusage" ADD COLUMN "workflow_run_id" TEXT NULL REFERENCES "core_workflowrun" ("id");
--
-- Create constraint tenant_token_usage_exactly_one_run on model tenanttokenusage
--
CREATE TABLE "new__core_tenanttokenusage" ("id" TEXT NOT NULL PRIMARY KEY, "idempotency_key" varchar(160) NOT NULL, "status" varchar(16) NOT NULL, "reserved_tokens" bigint unsigned NOT NULL CHECK ("reserved_tokens" >= 0), "consumed_tokens" bigint unsigned NOT NULL CHECK ("consumed_tokens" >= 0), "expires_at" TIMESTAMP NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_run_id" TEXT NULL REFERENCES "core_agentrun" ("id"), "task_id" TEXT NOT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "workflow_run_id" TEXT NULL REFERENCES "core_workflowrun" ("id"), CONSTRAINT "uniq_tenant_token_usage_idempotency" UNIQUE ("tenant_id", "idempotency_key"), CONSTRAINT "tenant_token_usage_exactly_one_run" CHECK ((("agent_run_id" IS NOT NULL AND "workflow_run_id" IS NULL) OR ("agent_run_id" IS NULL AND "workflow_run_id" IS NOT NULL))));
INSERT INTO "new__core_tenanttokenusage" ("id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "task_id", "tenant_id", "workflow_run_id") SELECT "id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "task_id", "tenant_id", "workflow_run_id" FROM "core_tenanttokenusage";
DROP TABLE "core_tenanttokenusage";
ALTER TABLE "new__core_tenanttokenusage" RENAME TO "core_tenanttokenusage";
CREATE INDEX "core_tenanttokenusage_agent_run_id_562c15dc" ON "core_tenanttokenusage" ("agent_run_id");
CREATE INDEX "core_tenanttokenusage_task_id_b948d773" ON "core_tenanttokenusage" ("task_id");
CREATE INDEX "core_tenanttokenusage_tenant_id_2d1f25b3" ON "core_tenanttokenusage" ("tenant_id");
CREATE INDEX "core_tenanttokenusage_workflow_run_id_9c0dee5e" ON "core_tenanttokenusage" ("workflow_run_id");
CREATE INDEX "tenant_token_usage_period_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "created_at");
-- 0020_token_usage_settlement_timestamp
--
-- Add field settled_at to tenanttokenusage
--
ALTER TABLE "core_tenanttokenusage" ADD COLUMN "settled_at" TIMESTAMP NULL;
-- 0021_token_usage_period_indexes
--
-- Remove index tenant_token_usage_period_idx from tenanttokenusage
--
DROP INDEX "tenant_token_usage_period_idx";
--
-- Create index tenant_token_settled_idx on field(s) tenant, status, settled_at of model tenanttokenusage
--
CREATE INDEX "tenant_token_settled_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "settled_at");
--
-- Create index tenant_token_reserved_idx on field(s) tenant, status, expires_at of model tenanttokenusage
--
CREATE INDEX "tenant_token_reserved_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "expires_at");
-- 0022_one_healthy_plugin_connection
--
-- Raw Python operation
--
-- THIS OPERATION CANNOT BE WRITTEN AS SQL
--
-- Create constraint uniq_healthy_plugin_connection on model pluginconnection
--
CREATE UNIQUE INDEX "uniq_healthy_plugin_connection" ON "core_pluginconnection" ("installation_id") WHERE "status" = 'healthy';
-- 0023_agent_plugin_registry
--
-- Create model AgentPluginDefinition
--
CREATE TABLE "core_agentplugindefinition" ("id" TEXT NOT NULL PRIMARY KEY, "plugin_key" varchar(100) NOT NULL, "name" varchar(120) NOT NULL, "description" text NOT NULL, "version" varchar(40) NOT NULL, "protocol" varchar(16) NOT NULL, "endpoint_url" varchar(500) NOT NULL, "auth_required" bool NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "input_schema" text NOT NULL CHECK ((JSON_VALID("input_schema") OR "input_schema" IS NULL)), "output_schema" text NOT NULL CHECK ((JSON_VALID("output_schema") OR "output_schema" IS NULL)), "timeout_seconds" smallint unsigned NOT NULL CHECK ("timeout_seconds" >= 0), "max_concurrency" smallint unsigned NOT NULL CHECK ("max_concurrency" >= 0), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_plugin_key_version" UNIQUE ("tenant_id", "plugin_key", "version"));
--
-- Create model AgentPluginInstallation
--
CREATE TABLE "core_agentplugininstallation" ("id" TEXT NOT NULL PRIMARY KEY, "plugin_version" varchar(40) NOT NULL, "status" varchar(16) NOT NULL, "granted_capabilities" text NOT NULL CHECK ((JSON_VALID("granted_capabilities") OR "granted_capabilities" IS NULL)), "credential_ref" varchar(200) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "installed_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "plugin_id" TEXT NOT NULL REFERENCES "core_agentplugindefinition" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_plugin_installation_per_project" UNIQUE ("project_id", "plugin_id"));
CREATE INDEX "core_agentplugindefinition_plugin_key_9f6e4684" ON "core_agentplugindefinition" ("plugin_key");
CREATE INDEX "core_agentplugindefinition_created_by_id_875582da" ON "core_agentplugindefinition" ("created_by_id");
CREATE INDEX "core_agentplugindefinition_tenant_id_e1663a50" ON "core_agentplugindefinition" ("tenant_id");
CREATE INDEX "core_agentplugininstallation_installed_by_id_8b1e7745" ON "core_agentplugininstallation" ("installed_by_id");
CREATE INDEX "core_agentplugininstallation_plugin_id_a0e7c249" ON "core_agentplugininstallation" ("plugin_id");
CREATE INDEX "core_agentplugininstallation_project_id_74d9b2d3" ON "core_agentplugininstallation" ("project_id");
CREATE INDEX "core_agentplugininstallation_tenant_id_8b552e6e" ON "core_agentplugininstallation" ("tenant_id");
-- 0024_audit_event_nullable_tenant
--
-- Alter field tenant on auditevent
--
CREATE TABLE "new__core_auditevent" ("tenant_id" TEXT NULL REFERENCES "core_tenant" ("id"), "id" TEXT NOT NULL PRIMARY KEY, "event_type" varchar(120) NOT NULL, "resource_type" varchar(80) NOT NULL, "resource_id" varchar(64) NOT NULL, "outcome" varchar(16) NOT NULL, "request_id" varchar(64) NOT NULL, "remote_addr" TEXT NULL, "user_agent" varchar(256) NOT NULL, "metadata" text NOT NULL CHECK ((JSON_VALID("metadata") OR "metadata" IS NULL)), "previous_hash" varchar(64) NOT NULL, "event_hash" varchar(64) NOT NULL UNIQUE, "created_at" TIMESTAMP NOT NULL, "actor_id" integer NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NULL REFERENCES "core_project" ("id"));
INSERT INTO "new__core_auditevent" ("id", "event_type", "resource_type", "resource_id", "outcome", "request_id", "remote_addr", "user_agent", "metadata", "previous_hash", "event_hash", "created_at", "actor_id", "project_id", "tenant_id") SELECT "id", "event_type", "resource_type", "resource_id", "outcome", "request_id", "remote_addr", "user_agent", "metadata", "previous_hash", "event_hash", "created_at", "actor_id", "project_id", "tenant_id" FROM "core_auditevent";
DROP TABLE "core_auditevent";
ALTER TABLE "new__core_auditevent" RENAME TO "core_auditevent";
CREATE INDEX "core_auditevent_tenant_id_43ff9a90" ON "core_auditevent" ("tenant_id");
CREATE INDEX "core_auditevent_actor_id_3bd0c673" ON "core_auditevent" ("actor_id");
CREATE INDEX "core_auditevent_project_id_f3f2c86f" ON "core_auditevent" ("project_id");
CREATE INDEX "audit_tenant_time_idx" ON "core_auditevent" ("tenant_id", "created_at" DESC);
CREATE INDEX "audit_tenant_type_idx" ON "core_auditevent" ("tenant_id", "event_type", "created_at" DESC);
-- 0025_knowledgedocument_vector_collection_and_more
--
-- Add field vector_collection to knowledgedocument
--
CREATE TABLE "new__core_knowledgedocument" ("vector_collection" varchar(200) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "title" varchar(240) NOT NULL, "content" text NOT NULL, "metadata" text NOT NULL CHECK ((JSON_VALID("metadata") OR "metadata" IS NULL)), "checksum" varchar(64) NOT NULL, "status" varchar(16) NOT NULL, "chunk_count" integer unsigned NOT NULL CHECK ("chunk_count" >= 0), "error" text NULL CHECK ((JSON_VALID("error") OR "error" IS NULL)), "ingested_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "knowledge_base_id" TEXT NOT NULL REFERENCES "core_knowledgebase" ("id"), "uploaded_by_id" integer NOT NULL REFERENCES "platform_users" ("id"));
INSERT INTO "new__core_knowledgedocument" ("id", "title", "content", "metadata", "checksum", "status", "chunk_count", "error", "ingested_at", "created_at", "updated_at", "knowledge_base_id", "uploaded_by_id", "vector_collection") SELECT "id", "title", "content", "metadata", "checksum", "status", "chunk_count", "error", "ingested_at", "created_at", "updated_at", "knowledge_base_id", "uploaded_by_id", '' FROM "core_knowledgedocument";
DROP TABLE "core_knowledgedocument";
ALTER TABLE "new__core_knowledgedocument" RENAME TO "core_knowledgedocument";
CREATE INDEX "core_knowledgedocument_knowledge_base_id_9963f00c" ON "core_knowledgedocument" ("knowledge_base_id");
CREATE INDEX "core_knowledgedocument_uploaded_by_id_be7eb163" ON "core_knowledgedocument" ("uploaded_by_id");
CREATE INDEX "knowledge_doc_status_idx" ON "core_knowledgedocument" ("knowledge_base_id", "status", "created_at");
--
-- Add field vector_dimensions to knowledgedocument
--
ALTER TABLE "core_knowledgedocument" ADD COLUMN "vector_dimensions" integer unsigned NULL CHECK ("vector_dimensions" >= 0);
-- 0026_delete_clusterdeploymentcheckpoint
--
-- Delete model ClusterDeploymentCheckpoint
--
DROP TABLE "core_clusterdeploymentcheckpoint";
-- 0027_agentteam_agentteamversion_agentteamrun_and_more
--
-- Create model AgentTeam
--
CREATE TABLE "core_agentteam" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "slug" varchar(80) NOT NULL, "description" text NOT NULL, "draft_config" text NOT NULL CHECK ((JSON_VALID("draft_config") OR "draft_config" IS NULL)), "current_version" integer unsigned NOT NULL CHECK ("current_version" >= 0), "is_archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create model AgentTeamVersion
--
CREATE TABLE "core_agentteamversion" ("id" TEXT NOT NULL PRIMARY KEY, "version" integer unsigned NOT NULL CHECK ("version" >= 0), "definition_snapshot" text NOT NULL CHECK ((JSON_VALID("definition_snapshot") OR "definition_snapshot" IS NULL)), "published_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "team_id" TEXT NOT NULL REFERENCES "core_agentteam" ("id"), "workflow_version_id" TEXT NOT NULL REFERENCES "core_workflowversion" ("id"));
--
-- Create model AgentTeamRun
--
CREATE TABLE "core_agentteamrun" ("id" TEXT NOT NULL PRIMARY KEY, "input_text" text NOT NULL, "output_text" text NOT NULL, "created_at" TIMESTAMP NOT NULL, "finished_at" TIMESTAMP NULL, "workflow_run_id" TEXT NOT NULL UNIQUE REFERENCES "core_workflowrun" ("id"), "team_version_id" TEXT NOT NULL REFERENCES "core_agentteamversion" ("id"));
--
-- Create constraint uniq_agent_team_slug_per_project on model agentteam
--
CREATE TABLE "new__core_agentteam" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "slug" varchar(80) NOT NULL, "description" text NOT NULL, "draft_config" text NOT NULL CHECK ((JSON_VALID("draft_config") OR "draft_config" IS NULL)), "current_version" integer unsigned NOT NULL CHECK ("current_version" >= 0), "is_archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_team_slug_per_project" UNIQUE ("project_id", "slug"));
INSERT INTO "new__core_agentteam" ("id", "name", "slug", "description", "draft_config", "current_version", "is_archived", "created_at", "updated_at", "created_by_id", "project_id", "tenant_id") SELECT "id", "name", "slug", "description", "draft_config", "current_version", "is_archived", "created_at", "updated_at", "created_by_id", "project_id", "tenant_id" FROM "core_agentteam";
DROP TABLE "core_agentteam";
ALTER TABLE "new__core_agentteam" RENAME TO "core_agentteam";
CREATE INDEX "core_agentteamversion_created_by_id_c95485e5" ON "core_agentteamversion" ("created_by_id");
CREATE INDEX "core_agentteamversion_team_id_0bf1d179" ON "core_agentteamversion" ("team_id");
CREATE INDEX "core_agentteamversion_workflow_version_id_c2701d74" ON "core_agentteamversion" ("workflow_version_id");
CREATE INDEX "core_agentteamrun_team_version_id_d1b926fd" ON "core_agentteamrun" ("team_version_id");
CREATE INDEX "core_agentteam_slug_2acbf5b7" ON "core_agentteam" ("slug");
CREATE INDEX "core_agentteam_created_by_id_6687c851" ON "core_agentteam" ("created_by_id");
CREATE INDEX "core_agentteam_project_id_7ba2f617" ON "core_agentteam" ("project_id");
CREATE INDEX "core_agentteam_tenant_id_37e30d6a" ON "core_agentteam" ("tenant_id");
--
-- Create constraint uniq_agent_team_version_number on model agentteamversion
--
CREATE TABLE "new__core_agentteamversion" ("id" TEXT NOT NULL PRIMARY KEY, "version" integer unsigned NOT NULL CHECK ("version" >= 0), "definition_snapshot" text NOT NULL CHECK ((JSON_VALID("definition_snapshot") OR "definition_snapshot" IS NULL)), "published_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "team_id" TEXT NOT NULL REFERENCES "core_agentteam" ("id"), "workflow_version_id" TEXT NOT NULL REFERENCES "core_workflowversion" ("id"), CONSTRAINT "uniq_agent_team_version_number" UNIQUE ("team_id", "version"));
INSERT INTO "new__core_agentteamversion" ("id", "version", "definition_snapshot", "published_at", "created_by_id", "team_id", "workflow_version_id") SELECT "id", "version", "definition_snapshot", "published_at", "created_by_id", "team_id", "workflow_version_id" FROM "core_agentteamversion";
DROP TABLE "core_agentteamversion";
ALTER TABLE "new__core_agentteamversion" RENAME TO "core_agentteamversion";
CREATE INDEX "core_agentteamversion_created_by_id_c95485e5" ON "core_agentteamversion" ("created_by_id");
CREATE INDEX "core_agentteamversion_team_id_0bf1d179" ON "core_agentteamversion" ("team_id");
CREATE INDEX "core_agentteamversion_workflow_version_id_c2701d74" ON "core_agentteamversion" ("workflow_version_id");
-- 0028_workflow_step_approval
--
-- Alter field status on workflowsteprun
--
CREATE TABLE "new__core_workflowsteprun" ("status" varchar(24) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "node_id" varchar(120) NOT NULL, "node_type" varchar(32) NOT NULL, "input_data" text NOT NULL CHECK ((JSON_VALID("input_data") OR "input_data" IS NULL)), "output_data" text NULL CHECK ((JSON_VALID("output_data") OR "output_data" IS NULL)), "error" text NULL CHECK ((JSON_VALID("error") OR "error" IS NULL)), "attempt" smallint unsigned NOT NULL CHECK ("attempt" >= 0), "started_at" TIMESTAMP NULL, "finished_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "workflow_run_id" TEXT NOT NULL REFERENCES "core_workflowrun" ("id"), CONSTRAINT "uniq_workflow_step_run_node" UNIQUE ("workflow_run_id", "node_id"));
INSERT INTO "new__core_workflowsteprun" ("id", "node_id", "node_type", "input_data", "output_data", "error", "attempt", "started_at", "finished_at", "created_at", "workflow_run_id", "status") SELECT "id", "node_id", "node_type", "input_data", "output_data", "error", "attempt", "started_at", "finished_at", "created_at", "workflow_run_id", "status" FROM "core_workflowsteprun";
DROP TABLE "core_workflowsteprun";
ALTER TABLE "new__core_workflowsteprun" RENAME TO "core_workflowsteprun";
CREATE INDEX "core_workflowsteprun_workflow_run_id_b0f4b856" ON "core_workflowsteprun" ("workflow_run_id");
-- 0029_workflow_run_deadline
--
-- Add field deadline_at to workflowrun
--
ALTER TABLE "core_workflowrun" ADD COLUMN "deadline_at" TIMESTAMP NULL;
-- 0030_firmware_update_job
--
-- Create model FirmwareUpdateJob
--
CREATE TABLE "core_firmwareupdatejob" ("id" TEXT NOT NULL PRIMARY KEY, "image_url" varchar(500) NOT NULL, "component" varchar(160) NOT NULL, "apply_time" varchar(32) NOT NULL, "targets" text NOT NULL CHECK ((JSON_VALID("targets") OR "targets" IS NULL)), "status" varchar(16) NOT NULL, "task_monitor" varchar(500) NOT NULL, "task_state" varchar(64) NOT NULL, "message" text NOT NULL, "idempotency_key" varchar(128) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "finished_at" TIMESTAMP NULL, "machine_id" TEXT NOT NULL REFERENCES "core_agentmachine" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "requested_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_firmware_job_idempotency" UNIQUE ("tenant_id", "machine_id", "idempotency_key"));
CREATE INDEX "core_firmwareupdatejob_machine_id_2242a825" ON "core_firmwareupdatejob" ("machine_id");
CREATE INDEX "core_firmwareupdatejob_project_id_6c90b331" ON "core_firmwareupdatejob" ("project_id");
CREATE INDEX "core_firmwareupdatejob_requested_by_id_738b2edd" ON "core_firmwareupdatejob" ("requested_by_id");
CREATE INDEX "core_firmwareupdatejob_tenant_id_90323ff6" ON "core_firmwareupdatejob" ("tenant_id");
-- 0031_infrastructure_network_profile
--
-- Create model InfrastructureNetworkProfile
--
CREATE TABLE "core_infrastructurenetworkprofile" ("id" TEXT NOT NULL PRIMARY KEY, "profile" text NOT NULL CHECK ((JSON_VALID("profile") OR "profile" IS NULL)), "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "updated_by_id" integer NULL REFERENCES "platform_users" ("id"));
--
-- Create constraint uniq_infra_network_profile_project on model infrastructurenetworkprofile
--
CREATE TABLE "new__core_infrastructurenetworkprofile" ("id" TEXT NOT NULL PRIMARY KEY, "profile" text NOT NULL CHECK ((JSON_VALID("profile") OR "profile" IS NULL)), "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "updated_by_id" integer NULL REFERENCES "platform_users" ("id"), CONSTRAINT "uniq_infra_network_profile_project" UNIQUE ("tenant_id", "project_id"));
INSERT INTO "new__core_infrastructurenetworkprofile" ("id", "profile", "created_at", "updated_at", "project_id", "tenant_id", "updated_by_id") SELECT "id", "profile", "created_at", "updated_at", "project_id", "tenant_id", "updated_by_id" FROM "core_infrastructurenetworkprofile";
DROP TABLE "core_infrastructurenetworkprofile";
ALTER TABLE "new__core_infrastructurenetworkprofile" RENAME TO "core_infrastructurenetworkprofile";
CREATE INDEX "core_infrastructurenetworkprofile_project_id_66739a25" ON "core_infrastructurenetworkprofile" ("project_id");
CREATE INDEX "core_infrastructurenetworkprofile_tenant_id_c616bb9f" ON "core_infrastructurenetworkprofile" ("tenant_id");
CREATE INDEX "core_infrastructurenetworkprofile_updated_by_id_beeab75a" ON "core_infrastructurenetworkprofile" ("updated_by_id");
-- 0032_agent_machine_inventory_revision
--
-- Add field inventory_revision to agentmachine
--
CREATE TABLE "new__core_agentmachine" ("inventory_revision" integer unsigned NOT NULL CHECK ("inventory_revision" >= 0), "id" TEXT NOT NULL PRIMARY KEY, "vendor" varchar(256) NOT NULL, "product_name" varchar(256) NOT NULL, "system_uuid" varchar(128) NOT NULL, "identity_material" text NOT NULL CHECK ((JSON_VALID("identity_material") OR "identity_material" IS NULL)), "identity_fingerprint" varchar(64) NOT NULL, "identity_status" varchar(16) NOT NULL, "identity_conflicts" text NOT NULL CHECK ((JSON_VALID("identity_conflicts") OR "identity_conflicts" IS NULL)), "hardware_inventory" text NOT NULL CHECK ((JSON_VALID("hardware_inventory") OR "hardware_inventory" IS NULL)), "inventory_source" varchar(48) NOT NULL, "observed_at" TIMESTAMP NULL, "bmc_credential_ref" varchar(160) NOT NULL, "bmh_name" varchar(253) NOT NULL, "capi_machine_name" varchar(253) NOT NULL, "kubernetes_node_name" varchar(253) NOT NULL, "lifecycle_state" varchar(32) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_node_id" TEXT NOT NULL UNIQUE REFERENCES "core_agentnode" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "bmc_endpoint" varchar(500) NOT NULL, "bmc_trust_bundle_ref" varchar(160) NOT NULL, "bmc_tls_verify" bool NOT NULL, "bmc_configured_at" TIMESTAMP NULL, "bmc_identity_evidence" text NOT NULL CHECK ((JSON_VALID("bmc_identity_evidence") OR "bmc_identity_evidence" IS NULL)));
INSERT INTO "new__core_agentmachine" ("id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint", "bmc_trust_bundle_ref", "bmc_tls_verify", "bmc_configured_at", "bmc_identity_evidence", "inventory_revision") SELECT "id", "vendor", "product_name", "system_uuid", "identity_material", "identity_fingerprint", "identity_status", "identity_conflicts", "hardware_inventory", "inventory_source", "observed_at", "bmc_credential_ref", "bmh_name", "capi_machine_name", "kubernetes_node_name", "lifecycle_state", "created_at", "updated_at", "agent_node_id", "project_id", "tenant_id", "bmc_endpoint", "bmc_trust_bundle_ref", "bmc_tls_verify", "bmc_configured_at", "bmc_identity_evidence", 0 FROM "core_agentmachine";
DROP TABLE "core_agentmachine";
ALTER TABLE "new__core_agentmachine" RENAME TO "core_agentmachine";
CREATE INDEX "core_agentmachine_project_id_22866b67" ON "core_agentmachine" ("project_id");
CREATE INDEX "core_agentmachine_tenant_id_f6be2e25" ON "core_agentmachine" ("tenant_id");
CREATE INDEX "machine_identity_idx" ON "core_agentmachine" ("tenant_id", "project_id", "identity_status");
CREATE INDEX "machine_observed_idx" ON "core_agentmachine" ("tenant_id", "project_id", "observed_at");
-- 0033_agent_plugin_manifest
--
-- Add field manifest to agentplugindefinition
--
CREATE TABLE "new__core_agentplugindefinition" ("manifest" text NOT NULL CHECK ((JSON_VALID("manifest") OR "manifest" IS NULL)), "id" TEXT NOT NULL PRIMARY KEY, "plugin_key" varchar(100) NOT NULL, "name" varchar(120) NOT NULL, "description" text NOT NULL, "version" varchar(40) NOT NULL, "protocol" varchar(16) NOT NULL, "endpoint_url" varchar(500) NOT NULL, "auth_required" bool NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "input_schema" text NOT NULL CHECK ((JSON_VALID("input_schema") OR "input_schema" IS NULL)), "output_schema" text NOT NULL CHECK ((JSON_VALID("output_schema") OR "output_schema" IS NULL)), "timeout_seconds" smallint unsigned NOT NULL CHECK ("timeout_seconds" >= 0), "max_concurrency" smallint unsigned NOT NULL CHECK ("max_concurrency" >= 0), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_plugin_key_version" UNIQUE ("tenant_id", "plugin_key", "version"));
INSERT INTO "new__core_agentplugindefinition" ("id", "plugin_key", "name", "description", "version", "protocol", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest") SELECT "id", "plugin_key", "name", "description", "version", "protocol", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", '{}' FROM "core_agentplugindefinition";
DROP TABLE "core_agentplugindefinition";
ALTER TABLE "new__core_agentplugindefinition" RENAME TO "core_agentplugindefinition";
CREATE INDEX "core_agentplugindefinition_plugin_key_9f6e4684" ON "core_agentplugindefinition" ("plugin_key");
CREATE INDEX "core_agentplugindefinition_created_by_id_875582da" ON "core_agentplugindefinition" ("created_by_id");
CREATE INDEX "core_agentplugindefinition_tenant_id_e1663a50" ON "core_agentplugindefinition" ("tenant_id");
-- 0033_backfill_command_deployment
--
-- Raw Python operation
--
-- THIS OPERATION CANNOT BE WRITTEN AS SQL
-- 0034_agent_capability_invocation
--
-- Create model AgentCapabilityInvocation
--
CREATE TABLE "core_agentcapabilityinvocation" ("id" TEXT NOT NULL PRIMARY KEY, "capability" varchar(100) NOT NULL, "protocol" varchar(16) NOT NULL, "status" varchar(16) NOT NULL, "error_code" varchar(64) NOT NULL, "latency_ms" integer unsigned NOT NULL CHECK ("latency_ms" >= 0), "created_at" TIMESTAMP NOT NULL, "installation_id" TEXT NOT NULL REFERENCES "core_agentplugininstallation" ("id"));
CREATE INDEX "core_agentcapabilityinvocation_installation_id_2260da42" ON "core_agentcapabilityinvocation" ("installation_id");
CREATE INDEX "agent_cap_invocation_idx" ON "core_agentcapabilityinvocation" ("installation_id", "capability", "created_at");
-- 0034_agentcommand_expired_status
--
-- Alter field status on agentcommand
--
-- (no-op)
-- 0035_merge_agent_plugin_and_command_deployment
-- 0036_merge_agent_marketplace_and_command_expiry
-- 0037_agent_plugin_installation_revision
--
-- Add field revision to agentplugininstallation
--
CREATE TABLE "new__core_agentplugininstallation" ("revision" integer unsigned NOT NULL CHECK ("revision" >= 0), "id" TEXT NOT NULL PRIMARY KEY, "plugin_version" varchar(40) NOT NULL, "status" varchar(16) NOT NULL, "granted_capabilities" text NOT NULL CHECK ((JSON_VALID("granted_capabilities") OR "granted_capabilities" IS NULL)), "credential_ref" varchar(200) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "installed_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "plugin_id" TEXT NOT NULL REFERENCES "core_agentplugindefinition" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_agent_plugin_installation_per_project" UNIQUE ("project_id", "plugin_id"));
INSERT INTO "new__core_agentplugininstallation" ("id", "plugin_version", "status", "granted_capabilities", "credential_ref", "created_at", "updated_at", "installed_by_id", "plugin_id", "project_id", "tenant_id", "revision") SELECT "id", "plugin_version", "status", "granted_capabilities", "credential_ref", "created_at", "updated_at", "installed_by_id", "plugin_id", "project_id", "tenant_id", 1 FROM "core_agentplugininstallation";
DROP TABLE "core_agentplugininstallation";
ALTER TABLE "new__core_agentplugininstallation" RENAME TO "core_agentplugininstallation";
CREATE INDEX "core_agentplugininstallation_installed_by_id_8b1e7745" ON "core_agentplugininstallation" ("installed_by_id");
CREATE INDEX "core_agentplugininstallation_plugin_id_a0e7c249" ON "core_agentplugininstallation" ("plugin_id");
CREATE INDEX "core_agentplugininstallation_project_id_74d9b2d3" ON "core_agentplugininstallation" ("project_id");
CREATE INDEX "core_agentplugininstallation_tenant_id_8b552e6e" ON "core_agentplugininstallation" ("tenant_id");
--
-- Alter field protocol on agentplugindefinition
--
CREATE TABLE "new__core_agentplugindefinition" ("id" TEXT NOT NULL PRIMARY KEY, "plugin_key" varchar(100) NOT NULL, "name" varchar(120) NOT NULL, "description" text NOT NULL, "version" varchar(40) NOT NULL, "endpoint_url" varchar(500) NOT NULL, "auth_required" bool NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "input_schema" text NOT NULL CHECK ((JSON_VALID("input_schema") OR "input_schema" IS NULL)), "output_schema" text NOT NULL CHECK ((JSON_VALID("output_schema") OR "output_schema" IS NULL)), "timeout_seconds" smallint unsigned NOT NULL CHECK ("timeout_seconds" >= 0), "max_concurrency" smallint unsigned NOT NULL CHECK ("max_concurrency" >= 0), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "manifest" text NOT NULL CHECK ((JSON_VALID("manifest") OR "manifest" IS NULL)), "protocol" varchar(32) NOT NULL, CONSTRAINT "uniq_agent_plugin_key_version" UNIQUE ("tenant_id", "plugin_key", "version"));
INSERT INTO "new__core_agentplugindefinition" ("id", "plugin_key", "name", "description", "version", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest", "protocol") SELECT "id", "plugin_key", "name", "description", "version", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest", "protocol" FROM "core_agentplugindefinition";
DROP TABLE "core_agentplugindefinition";
ALTER TABLE "new__core_agentplugindefinition" RENAME TO "core_agentplugindefinition";
CREATE INDEX "core_agentplugindefinition_plugin_key_9f6e4684" ON "core_agentplugindefinition" ("plugin_key");
CREATE INDEX "core_agentplugindefinition_created_by_id_875582da" ON "core_agentplugindefinition" ("created_by_id");
CREATE INDEX "core_agentplugindefinition_tenant_id_e1663a50" ON "core_agentplugindefinition" ("tenant_id");
-- 0038_widen_capability_invocation_protocol
--
-- Alter field protocol on agentcapabilityinvocation
--
CREATE TABLE "new__core_agentcapabilityinvocation" ("protocol" varchar(32) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "capability" varchar(100) NOT NULL, "status" varchar(16) NOT NULL, "error_code" varchar(64) NOT NULL, "latency_ms" integer unsigned NOT NULL CHECK ("latency_ms" >= 0), "created_at" TIMESTAMP NOT NULL, "installation_id" TEXT NOT NULL REFERENCES "core_agentplugininstallation" ("id"));
INSERT INTO "new__core_agentcapabilityinvocation" ("id", "capability", "status", "error_code", "latency_ms", "created_at", "installation_id", "protocol") SELECT "id", "capability", "status", "error_code", "latency_ms", "created_at", "installation_id", "protocol" FROM "core_agentcapabilityinvocation";
DROP TABLE "core_agentcapabilityinvocation";
ALTER TABLE "new__core_agentcapabilityinvocation" RENAME TO "core_agentcapabilityinvocation";
CREATE INDEX "core_agentcapabilityinvocation_installation_id_2260da42" ON "core_agentcapabilityinvocation" ("installation_id");
CREATE INDEX "agent_cap_invocation_idx" ON "core_agentcapabilityinvocation" ("installation_id", "capability", "created_at");
-- 0039_settings_storage
--
-- Create model TenantSetting
--
CREATE TABLE "core_tenantsetting" ("id" TEXT NOT NULL PRIMARY KEY, "section" varchar(32) NOT NULL, "values" text NOT NULL CHECK ((JSON_VALID("values") OR "values" IS NULL)), "updated_at" TIMESTAMP NOT NULL, "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "updated_by_id" integer NULL REFERENCES "platform_users" ("id"), CONSTRAINT "uniq_tenant_setting_section" UNIQUE ("tenant_id", "section"));
--
-- Create model ProjectSetting
--
CREATE TABLE "core_projectsetting" ("id" TEXT NOT NULL PRIMARY KEY, "section" varchar(32) NOT NULL, "values" text NOT NULL CHECK ((JSON_VALID("values") OR "values" IS NULL)), "updated_at" TIMESTAMP NOT NULL, "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "updated_by_id" integer NULL REFERENCES "platform_users" ("id"), CONSTRAINT "uniq_project_setting_section" UNIQUE ("project_id", "section"));
--
-- Create model UserPreference
--
CREATE TABLE "core_userpreference" ("id" TEXT NOT NULL PRIMARY KEY, "namespace" varchar(32) NOT NULL, "values" text NOT NULL CHECK ((JSON_VALID("values") OR "values" IS NULL)), "updated_at" TIMESTAMP NOT NULL, "user_id" integer NOT NULL REFERENCES "platform_users" ("id"), "updated_by_id" integer NULL REFERENCES "platform_users" ("id"), CONSTRAINT "uniq_user_preference_namespace" UNIQUE ("user_id", "namespace"));
CREATE INDEX "core_tenantsetting_tenant_id_2d89fbb5" ON "core_tenantsetting" ("tenant_id");
CREATE INDEX "core_tenantsetting_updated_by_id_557f61cb" ON "core_tenantsetting" ("updated_by_id");
CREATE INDEX "core_projectsetting_tenant_id_093c32af" ON "core_projectsetting" ("tenant_id");
CREATE INDEX "core_projectsetting_project_id_bccc6d35" ON "core_projectsetting" ("project_id");
CREATE INDEX "core_projectsetting_updated_by_id_a8a97180" ON "core_projectsetting" ("updated_by_id");
CREATE INDEX "core_userpreference_user_id_99465d98" ON "core_userpreference" ("user_id");
CREATE INDEX "core_userpreference_updated_by_id_ee5ab70f" ON "core_userpreference" ("updated_by_id");
-- 0040_account_profile_feedback
--
-- Create model UserAccountProfile
--
CREATE TABLE "core_useraccountprofile" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "display_name" varchar(160) NOT NULL, "timezone" varchar(64) NOT NULL, "locale" varchar(16) NOT NULL, "updated_at" TIMESTAMP NOT NULL, "updated_by_id" integer NULL REFERENCES "platform_users" ("id"), "user_id" integer NOT NULL UNIQUE REFERENCES "platform_users" ("id"));
--
-- Create model FeedbackEntry
--
CREATE TABLE "core_feedbackentry" ("id" TEXT NOT NULL PRIMARY KEY, "category" varchar(32) NOT NULL, "severity" varchar(16) NOT NULL, "body" text NOT NULL, "created_at" TIMESTAMP NOT NULL, "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "user_id" integer NOT NULL REFERENCES "platform_users" ("id"));
CREATE INDEX "core_useraccountprofile_updated_by_id_99682714" ON "core_useraccountprofile" ("updated_by_id");
CREATE INDEX "core_feedbackentry_tenant_id_225f86d0" ON "core_feedbackentry" ("tenant_id");
CREATE INDEX "core_feedbackentry_user_id_18f1138b" ON "core_feedbackentry" ("user_id");
-- 0041_data_export_and_deletion_requests
--
-- Create model DataExportJob
--
CREATE TABLE "core_dataexportjob" ("id" TEXT NOT NULL PRIMARY KEY, "scope" text NOT NULL CHECK ((JSON_VALID("scope") OR "scope" IS NULL)), "status" varchar(16) NOT NULL, "idempotency_key" varchar(128) NOT NULL, "row_count" integer unsigned NOT NULL CHECK ("row_count" >= 0), "file_ref" varchar(500) NOT NULL, "expires_at" TIMESTAMP NULL, "error_code" varchar(80) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "project_id" TEXT NULL REFERENCES "core_project" ("id"), "requested_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_data_export_idempotency" UNIQUE ("tenant_id", "idempotency_key"));
--
-- Create model DeletionRequest
--
CREATE TABLE "core_deletionrequest" ("id" TEXT NOT NULL PRIMARY KEY, "scope" text NOT NULL CHECK ((JSON_VALID("scope") OR "scope" IS NULL)), "status" varchar(16) NOT NULL, "idempotency_key" varchar(128) NOT NULL, "executed_at" TIMESTAMP NULL, "audit_ref" varchar(64) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "project_id" TEXT NULL REFERENCES "core_project" ("id"), "requested_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_deletion_request_idempotency" UNIQUE ("tenant_id", "idempotency_key"));
CREATE INDEX "core_dataexportjob_project_id_6b74c7b3" ON "core_dataexportjob" ("project_id");
CREATE INDEX "core_dataexportjob_requested_by_id_b54efa70" ON "core_dataexportjob" ("requested_by_id");
CREATE INDEX "core_dataexportjob_tenant_id_7c777af1" ON "core_dataexportjob" ("tenant_id");
CREATE INDEX "core_deletionrequest_project_id_35b8c21a" ON "core_deletionrequest" ("project_id");
CREATE INDEX "core_deletionrequest_requested_by_id_9dcf16ea" ON "core_deletionrequest" ("requested_by_id");
CREATE INDEX "core_deletionrequest_tenant_id_50c87dda" ON "core_deletionrequest" ("tenant_id");
-- 0042_archive_action
--
-- Create model ArchiveAction
--
CREATE TABLE "core_archiveaction" ("id" TEXT NOT NULL PRIMARY KEY, "item_type" varchar(40) NOT NULL, "item_id" varchar(64) NOT NULL, "action" varchar(10) NOT NULL, "idempotency_key" varchar(128) NOT NULL, "created_at" TIMESTAMP NOT NULL, "actor_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_archive_action_per_object_action" UNIQUE ("tenant_id", "item_type", "item_id", "action"));
CREATE INDEX "core_archiveaction_actor_id_060fac02" ON "core_archiveaction" ("actor_id");
CREATE INDEX "core_archiveaction_project_id_4c03c2e5" ON "core_archiveaction" ("project_id");
CREATE INDEX "core_archiveaction_tenant_id_0e8ea1a1" ON "core_archiveaction" ("tenant_id");
-- 0043_modelprovider_deployment_region
--
-- Add field deployment_region to modelprovider
--
CREATE TABLE "new__core_modelprovider" ("deployment_region" varchar(32) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "provider_type" varchar(32) NOT NULL, "base_url" varchar(500) NOT NULL, "credential_ref" varchar(160) NOT NULL, "config" text NOT NULL CHECK ((JSON_VALID("config") OR "config" IS NULL)), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_model_provider_name_per_tenant" UNIQUE ("tenant_id", "name"));
INSERT INTO "new__core_modelprovider" ("id", "name", "provider_type", "base_url", "credential_ref", "config", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "deployment_region") SELECT "id", "name", "provider_type", "base_url", "credential_ref", "config", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", '' FROM "core_modelprovider";
DROP TABLE "core_modelprovider";
ALTER TABLE "new__core_modelprovider" RENAME TO "core_modelprovider";
CREATE INDEX "core_modelprovider_created_by_id_d65969a2" ON "core_modelprovider" ("created_by_id");
CREATE INDEX "core_modelprovider_tenant_id_7abf3c0d" ON "core_modelprovider" ("tenant_id");
-- 0044_billing_plan_subscription
--
-- Create model Plan
--
CREATE TABLE "core_plan" ("id" TEXT NOT NULL PRIMARY KEY, "code" varchar(64) NOT NULL UNIQUE, "name" varchar(120) NOT NULL, "description" text NOT NULL, "interval" varchar(16) NOT NULL, "price_cents" bigint unsigned NOT NULL CHECK ("price_cents" >= 0), "currency" varchar(8) NOT NULL, "token_quota_daily" bigint unsigned NOT NULL CHECK ("token_quota_daily" >= 0), "token_quota_monthly" bigint unsigned NOT NULL CHECK ("token_quota_monthly" >= 0), "features" text NOT NULL CHECK ((JSON_VALID("features") OR "features" IS NULL)), "region_scope" varchar(16) NOT NULL, "active" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL);
--
-- Create model Subscription
--
CREATE TABLE "core_subscription" ("id" TEXT NOT NULL PRIMARY KEY, "status" varchar(16) NOT NULL, "started_at" TIMESTAMP NULL, "current_period_start" TIMESTAMP NULL, "current_period_end" TIMESTAMP NULL, "cancel_at_period_end" bool NOT NULL, "renewal_mode" varchar(16) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "plan_id" TEXT NOT NULL REFERENCES "core_plan" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create constraint uniq_subscription_tenant_plan on model subscription
--
CREATE TABLE "new__core_subscription" ("id" TEXT NOT NULL PRIMARY KEY, "status" varchar(16) NOT NULL, "started_at" TIMESTAMP NULL, "current_period_start" TIMESTAMP NULL, "current_period_end" TIMESTAMP NULL, "cancel_at_period_end" bool NOT NULL, "renewal_mode" varchar(16) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "plan_id" TEXT NOT NULL REFERENCES "core_plan" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_subscription_tenant_plan" UNIQUE ("tenant_id", "plan_id"));
INSERT INTO "new__core_subscription" ("id", "status", "started_at", "current_period_start", "current_period_end", "cancel_at_period_end", "renewal_mode", "created_at", "updated_at", "plan_id", "tenant_id") SELECT "id", "status", "started_at", "current_period_start", "current_period_end", "cancel_at_period_end", "renewal_mode", "created_at", "updated_at", "plan_id", "tenant_id" FROM "core_subscription";
DROP TABLE "core_subscription";
ALTER TABLE "new__core_subscription" RENAME TO "core_subscription";
CREATE INDEX "core_subscription_plan_id_0e74cf05" ON "core_subscription" ("plan_id");
CREATE INDEX "core_subscription_tenant_id_aa03a105" ON "core_subscription" ("tenant_id");
-- 0045_billing_order_entitlement_grant
--
-- Create model Order
--
CREATE TABLE "core_order" ("id" TEXT NOT NULL PRIMARY KEY, "status" varchar(16) NOT NULL, "amount_cents" bigint unsigned NOT NULL CHECK ("amount_cents" >= 0), "currency" varchar(8) NOT NULL, "channel" varchar(16) NOT NULL, "provider_trade_no" varchar(128) NOT NULL, "paid_at" TIMESTAMP NULL, "idempotency_key" varchar(160) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "plan_id" TEXT NOT NULL REFERENCES "core_plan" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create model EntitlementGrant
--
CREATE TABLE "core_entitlementgrant" ("id" TEXT NOT NULL PRIMARY KEY, "period" varchar(16) NOT NULL, "tokens" bigint unsigned NOT NULL CHECK ("tokens" >= 0), "expires_at" TIMESTAMP NULL, "reason" varchar(160) NOT NULL, "created_at" TIMESTAMP NOT NULL, "order_id" TEXT NOT NULL UNIQUE REFERENCES "core_order" ("id"), "subscription_id" TEXT NULL REFERENCES "core_subscription" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create constraint uniq_order_idempotency_per_tenant on model order
--
CREATE TABLE "new__core_order" ("id" TEXT NOT NULL PRIMARY KEY, "status" varchar(16) NOT NULL, "amount_cents" bigint unsigned NOT NULL CHECK ("amount_cents" >= 0), "currency" varchar(8) NOT NULL, "channel" varchar(16) NOT NULL, "provider_trade_no" varchar(128) NOT NULL, "paid_at" TIMESTAMP NULL, "idempotency_key" varchar(160) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "plan_id" TEXT NOT NULL REFERENCES "core_plan" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_order_idempotency_per_tenant" UNIQUE ("tenant_id", "idempotency_key"));
INSERT INTO "new__core_order" ("id", "status", "amount_cents", "currency", "channel", "provider_trade_no", "paid_at", "idempotency_key", "created_at", "updated_at", "created_by_id", "plan_id", "tenant_id") SELECT "id", "status", "amount_cents", "currency", "channel", "provider_trade_no", "paid_at", "idempotency_key", "created_at", "updated_at", "created_by_id", "plan_id", "tenant_id" FROM "core_order";
DROP TABLE "core_order";
ALTER TABLE "new__core_order" RENAME TO "core_order";
CREATE INDEX "core_entitlementgrant_subscription_id_e1190ca5" ON "core_entitlementgrant" ("subscription_id");
CREATE INDEX "core_entitlementgrant_tenant_id_e75cc01e" ON "core_entitlementgrant" ("tenant_id");
CREATE INDEX "core_order_created_by_id_1264a02d" ON "core_order" ("created_by_id");
CREATE INDEX "core_order_plan_id_ec2af184" ON "core_order" ("plan_id");
CREATE INDEX "core_order_tenant_id_8c26156f" ON "core_order" ("tenant_id");
--
-- Create constraint uniq_order_provider_trade_no on model order
--
CREATE UNIQUE INDEX "uniq_order_provider_trade_no" ON "core_order" ("channel", "provider_trade_no") WHERE "provider_trade_no" > '';
-- 0046_token_usage_scope_ref
--
-- Alter field task on tenanttokenusage
--
CREATE TABLE "new__core_tenanttokenusage" ("task_id" TEXT NULL REFERENCES "core_task" ("id"), "id" TEXT NOT NULL PRIMARY KEY, "idempotency_key" varchar(160) NOT NULL, "status" varchar(16) NOT NULL, "reserved_tokens" bigint unsigned NOT NULL CHECK ("reserved_tokens" >= 0), "consumed_tokens" bigint unsigned NOT NULL CHECK ("consumed_tokens" >= 0), "expires_at" TIMESTAMP NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_run_id" TEXT NULL REFERENCES "core_agentrun" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "workflow_run_id" TEXT NULL REFERENCES "core_workflowrun" ("id"), "settled_at" TIMESTAMP NULL, CONSTRAINT "uniq_tenant_token_usage_idempotency" UNIQUE ("tenant_id", "idempotency_key"), CONSTRAINT "tenant_token_usage_exactly_one_run" CHECK ((("agent_run_id" IS NOT NULL AND "workflow_run_id" IS NULL) OR ("agent_run_id" IS NULL AND "workflow_run_id" IS NOT NULL))));
INSERT INTO "new__core_tenanttokenusage" ("id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "tenant_id", "workflow_run_id", "settled_at", "task_id") SELECT "id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "tenant_id", "workflow_run_id", "settled_at", "task_id" FROM "core_tenanttokenusage";
DROP TABLE "core_tenanttokenusage";
ALTER TABLE "new__core_tenanttokenusage" RENAME TO "core_tenanttokenusage";
CREATE INDEX "core_tenanttokenusage_task_id_b948d773" ON "core_tenanttokenusage" ("task_id");
CREATE INDEX "core_tenanttokenusage_agent_run_id_562c15dc" ON "core_tenanttokenusage" ("agent_run_id");
CREATE INDEX "core_tenanttokenusage_tenant_id_2d1f25b3" ON "core_tenanttokenusage" ("tenant_id");
CREATE INDEX "core_tenanttokenusage_workflow_run_id_9c0dee5e" ON "core_tenanttokenusage" ("workflow_run_id");
CREATE INDEX "tenant_token_settled_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "settled_at");
CREATE INDEX "tenant_token_reserved_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "expires_at");
--
-- Add field scope_ref to tenanttokenusage
--
ALTER TABLE "core_tenanttokenusage" ADD COLUMN "scope_ref" varchar(160) NULL;
--
-- Remove constraint tenant_token_usage_exactly_one_run from model tenanttokenusage
--
CREATE TABLE "new__core_tenanttokenusage" ("id" TEXT NOT NULL PRIMARY KEY, "idempotency_key" varchar(160) NOT NULL, "status" varchar(16) NOT NULL, "reserved_tokens" bigint unsigned NOT NULL CHECK ("reserved_tokens" >= 0), "consumed_tokens" bigint unsigned NOT NULL CHECK ("consumed_tokens" >= 0), "expires_at" TIMESTAMP NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_run_id" TEXT NULL REFERENCES "core_agentrun" ("id"), "task_id" TEXT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "workflow_run_id" TEXT NULL REFERENCES "core_workflowrun" ("id"), "settled_at" TIMESTAMP NULL, "scope_ref" varchar(160) NULL, CONSTRAINT "uniq_tenant_token_usage_idempotency" UNIQUE ("tenant_id", "idempotency_key"));
INSERT INTO "new__core_tenanttokenusage" ("id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "task_id", "tenant_id", "workflow_run_id", "settled_at", "scope_ref") SELECT "id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "task_id", "tenant_id", "workflow_run_id", "settled_at", "scope_ref" FROM "core_tenanttokenusage";
DROP TABLE "core_tenanttokenusage";
ALTER TABLE "new__core_tenanttokenusage" RENAME TO "core_tenanttokenusage";
CREATE INDEX "core_tenanttokenusage_agent_run_id_562c15dc" ON "core_tenanttokenusage" ("agent_run_id");
CREATE INDEX "core_tenanttokenusage_task_id_b948d773" ON "core_tenanttokenusage" ("task_id");
CREATE INDEX "core_tenanttokenusage_tenant_id_2d1f25b3" ON "core_tenanttokenusage" ("tenant_id");
CREATE INDEX "core_tenanttokenusage_workflow_run_id_9c0dee5e" ON "core_tenanttokenusage" ("workflow_run_id");
CREATE INDEX "tenant_token_settled_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "settled_at");
CREATE INDEX "tenant_token_reserved_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "expires_at");
--
-- Create constraint tenant_token_usage_exactly_one_run on model tenanttokenusage
--
CREATE TABLE "new__core_tenanttokenusage" ("id" TEXT NOT NULL PRIMARY KEY, "idempotency_key" varchar(160) NOT NULL, "status" varchar(16) NOT NULL, "reserved_tokens" bigint unsigned NOT NULL CHECK ("reserved_tokens" >= 0), "consumed_tokens" bigint unsigned NOT NULL CHECK ("consumed_tokens" >= 0), "expires_at" TIMESTAMP NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "agent_run_id" TEXT NULL REFERENCES "core_agentrun" ("id"), "task_id" TEXT NULL REFERENCES "core_task" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "workflow_run_id" TEXT NULL REFERENCES "core_workflowrun" ("id"), "settled_at" TIMESTAMP NULL, "scope_ref" varchar(160) NULL, CONSTRAINT "uniq_tenant_token_usage_idempotency" UNIQUE ("tenant_id", "idempotency_key"), CONSTRAINT "tenant_token_usage_exactly_one_run" CHECK ((("agent_run_id" IS NOT NULL AND "workflow_run_id" IS NULL) OR ("agent_run_id" IS NULL AND "workflow_run_id" IS NOT NULL) OR ("agent_run_id" IS NULL AND "scope_ref" IS NOT NULL AND "workflow_run_id" IS NULL))));
INSERT INTO "new__core_tenanttokenusage" ("id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "task_id", "tenant_id", "workflow_run_id", "settled_at", "scope_ref") SELECT "id", "idempotency_key", "status", "reserved_tokens", "consumed_tokens", "expires_at", "created_at", "updated_at", "agent_run_id", "task_id", "tenant_id", "workflow_run_id", "settled_at", "scope_ref" FROM "core_tenanttokenusage";
DROP TABLE "core_tenanttokenusage";
ALTER TABLE "new__core_tenanttokenusage" RENAME TO "core_tenanttokenusage";
CREATE INDEX "core_tenanttokenusage_agent_run_id_562c15dc" ON "core_tenanttokenusage" ("agent_run_id");
CREATE INDEX "core_tenanttokenusage_task_id_b948d773" ON "core_tenanttokenusage" ("task_id");
CREATE INDEX "core_tenanttokenusage_tenant_id_2d1f25b3" ON "core_tenanttokenusage" ("tenant_id");
CREATE INDEX "core_tenanttokenusage_workflow_run_id_9c0dee5e" ON "core_tenanttokenusage" ("workflow_run_id");
CREATE INDEX "tenant_token_settled_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "settled_at");
CREATE INDEX "tenant_token_reserved_idx" ON "core_tenanttokenusage" ("tenant_id", "status", "expires_at");
-- 0048_subscription_lifecycle
--
-- Add field trial_days to plan
--
CREATE TABLE "new__core_plan" ("trial_days" smallint unsigned NOT NULL CHECK ("trial_days" >= 0), "id" TEXT NOT NULL PRIMARY KEY, "code" varchar(64) NOT NULL UNIQUE, "name" varchar(120) NOT NULL, "description" text NOT NULL, "interval" varchar(16) NOT NULL, "price_cents" bigint unsigned NOT NULL CHECK ("price_cents" >= 0), "currency" varchar(8) NOT NULL, "token_quota_daily" bigint unsigned NOT NULL CHECK ("token_quota_daily" >= 0), "token_quota_monthly" bigint unsigned NOT NULL CHECK ("token_quota_monthly" >= 0), "features" text NOT NULL CHECK ((JSON_VALID("features") OR "features" IS NULL)), "region_scope" varchar(16) NOT NULL, "active" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL);
INSERT INTO "new__core_plan" ("id", "code", "name", "description", "interval", "price_cents", "currency", "token_quota_daily", "token_quota_monthly", "features", "region_scope", "active", "created_at", "updated_at", "trial_days") SELECT "id", "code", "name", "description", "interval", "price_cents", "currency", "token_quota_daily", "token_quota_monthly", "features", "region_scope", "active", "created_at", "updated_at", 0 FROM "core_plan";
DROP TABLE "core_plan";
ALTER TABLE "new__core_plan" RENAME TO "core_plan";
--
-- Add field token_quota_daily_expired to plan
--
CREATE TABLE "new__core_plan" ("id" TEXT NOT NULL PRIMARY KEY, "code" varchar(64) NOT NULL UNIQUE, "name" varchar(120) NOT NULL, "description" text NOT NULL, "interval" varchar(16) NOT NULL, "price_cents" bigint unsigned NOT NULL CHECK ("price_cents" >= 0), "currency" varchar(8) NOT NULL, "token_quota_daily" bigint unsigned NOT NULL CHECK ("token_quota_daily" >= 0), "token_quota_monthly" bigint unsigned NOT NULL CHECK ("token_quota_monthly" >= 0), "features" text NOT NULL CHECK ((JSON_VALID("features") OR "features" IS NULL)), "region_scope" varchar(16) NOT NULL, "active" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "trial_days" smallint unsigned NOT NULL CHECK ("trial_days" >= 0), "token_quota_daily_expired" bigint unsigned NOT NULL CHECK ("token_quota_daily_expired" >= 0));
INSERT INTO "new__core_plan" ("id", "code", "name", "description", "interval", "price_cents", "currency", "token_quota_daily", "token_quota_monthly", "features", "region_scope", "active", "created_at", "updated_at", "trial_days", "token_quota_daily_expired") SELECT "id", "code", "name", "description", "interval", "price_cents", "currency", "token_quota_daily", "token_quota_monthly", "features", "region_scope", "active", "created_at", "updated_at", "trial_days", 0 FROM "core_plan";
DROP TABLE "core_plan";
ALTER TABLE "new__core_plan" RENAME TO "core_plan";
--
-- Add field token_quota_monthly_expired to plan
--
CREATE TABLE "new__core_plan" ("id" TEXT NOT NULL PRIMARY KEY, "code" varchar(64) NOT NULL UNIQUE, "name" varchar(120) NOT NULL, "description" text NOT NULL, "interval" varchar(16) NOT NULL, "price_cents" bigint unsigned NOT NULL CHECK ("price_cents" >= 0), "currency" varchar(8) NOT NULL, "token_quota_daily" bigint unsigned NOT NULL CHECK ("token_quota_daily" >= 0), "token_quota_monthly" bigint unsigned NOT NULL CHECK ("token_quota_monthly" >= 0), "features" text NOT NULL CHECK ((JSON_VALID("features") OR "features" IS NULL)), "region_scope" varchar(16) NOT NULL, "active" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "trial_days" smallint unsigned NOT NULL CHECK ("trial_days" >= 0), "token_quota_daily_expired" bigint unsigned NOT NULL CHECK ("token_quota_daily_expired" >= 0), "token_quota_monthly_expired" bigint unsigned NOT NULL CHECK ("token_quota_monthly_expired" >= 0));
INSERT INTO "new__core_plan" ("id", "code", "name", "description", "interval", "price_cents", "currency", "token_quota_daily", "token_quota_monthly", "features", "region_scope", "active", "created_at", "updated_at", "trial_days", "token_quota_daily_expired", "token_quota_monthly_expired") SELECT "id", "code", "name", "description", "interval", "price_cents", "currency", "token_quota_daily", "token_quota_monthly", "features", "region_scope", "active", "created_at", "updated_at", "trial_days", "token_quota_daily_expired", 0 FROM "core_plan";
DROP TABLE "core_plan";
ALTER TABLE "new__core_plan" RENAME TO "core_plan";
--
-- Alter field status on subscription
--
-- (no-op)
-- 0049_payment_transaction_audit
--
-- Create model PaymentTransactionAudit
--
CREATE TABLE "core_paymenttransactionaudit" ("id" TEXT NOT NULL PRIMARY KEY, "channel" varchar(16) NOT NULL, "direction" varchar(16) NOT NULL, "event_type" varchar(80) NOT NULL, "status" varchar(32) NOT NULL, "amount_cents" bigint unsigned NOT NULL CHECK ("amount_cents" >= 0), "provider_trade_no" varchar(128) NOT NULL, "error_code" varchar(80) NOT NULL, "metadata" text NOT NULL CHECK ((JSON_VALID("metadata") OR "metadata" IS NULL)), "created_at" TIMESTAMP NOT NULL, "order_id" TEXT NULL REFERENCES "core_order" ("id"), "tenant_id" TEXT NULL REFERENCES "core_tenant" ("id"));
CREATE INDEX "core_paymenttransactionaudit_order_id_0801aa1b" ON "core_paymenttransactionaudit" ("order_id");
CREATE INDEX "core_paymenttransactionaudit_tenant_id_d5665294" ON "core_paymenttransactionaudit" ("tenant_id");
-- 0050_entitlement_reclaim
--
-- Add field refunded_cents to order
--
CREATE TABLE "new__core_order" ("refunded_cents" bigint unsigned NOT NULL CHECK ("refunded_cents" >= 0), "id" TEXT NOT NULL PRIMARY KEY, "status" varchar(16) NOT NULL, "amount_cents" bigint unsigned NOT NULL CHECK ("amount_cents" >= 0), "currency" varchar(8) NOT NULL, "channel" varchar(16) NOT NULL, "provider_trade_no" varchar(128) NOT NULL, "paid_at" TIMESTAMP NULL, "idempotency_key" varchar(160) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "plan_id" TEXT NOT NULL REFERENCES "core_plan" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_order_idempotency_per_tenant" UNIQUE ("tenant_id", "idempotency_key"));
INSERT INTO "new__core_order" ("id", "status", "amount_cents", "currency", "channel", "provider_trade_no", "paid_at", "idempotency_key", "created_at", "updated_at", "created_by_id", "plan_id", "tenant_id", "refunded_cents") SELECT "id", "status", "amount_cents", "currency", "channel", "provider_trade_no", "paid_at", "idempotency_key", "created_at", "updated_at", "created_by_id", "plan_id", "tenant_id", 0 FROM "core_order";
DROP TABLE "core_order";
ALTER TABLE "new__core_order" RENAME TO "core_order";
CREATE UNIQUE INDEX "uniq_order_provider_trade_no" ON "core_order" ("channel", "provider_trade_no") WHERE "provider_trade_no" > '';
CREATE INDEX "core_order_created_by_id_1264a02d" ON "core_order" ("created_by_id");
CREATE INDEX "core_order_plan_id_ec2af184" ON "core_order" ("plan_id");
CREATE INDEX "core_order_tenant_id_8c26156f" ON "core_order" ("tenant_id");
--
-- Alter field status on order
--
-- (no-op)
--
-- Create model EntitlementReclaim
--
CREATE TABLE "core_entitlementreclaim" ("id" TEXT NOT NULL PRIMARY KEY, "refund_reference" varchar(160) NOT NULL UNIQUE, "refund_cents" bigint unsigned NOT NULL CHECK ("refund_cents" >= 0), "tokens" bigint unsigned NOT NULL CHECK ("tokens" >= 0), "monthly_limit_before" bigint unsigned NOT NULL CHECK ("monthly_limit_before" >= 0), "monthly_limit_after" bigint unsigned NOT NULL CHECK ("monthly_limit_after" >= 0), "reason" varchar(160) NOT NULL, "created_at" TIMESTAMP NOT NULL, "grant_id" TEXT NULL REFERENCES "core_entitlementgrant" ("id"), "order_id" TEXT NOT NULL REFERENCES "core_order" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
CREATE INDEX "core_entitlementreclaim_grant_id_342602a4" ON "core_entitlementreclaim" ("grant_id");
CREATE INDEX "core_entitlementreclaim_order_id_84db8830" ON "core_entitlementreclaim" ("order_id");
CREATE INDEX "core_entitlementreclaim_tenant_id_4d1fa2d6" ON "core_entitlementreclaim" ("tenant_id");
-- 0051_order_payment_instruction
--
-- Add field payment_instruction to order
--
CREATE TABLE "new__core_order" ("payment_instruction" text NOT NULL CHECK ((JSON_VALID("payment_instruction") OR "payment_instruction" IS NULL)), "id" TEXT NOT NULL PRIMARY KEY, "status" varchar(16) NOT NULL, "amount_cents" bigint unsigned NOT NULL CHECK ("amount_cents" >= 0), "currency" varchar(8) NOT NULL, "channel" varchar(16) NOT NULL, "provider_trade_no" varchar(128) NOT NULL, "paid_at" TIMESTAMP NULL, "idempotency_key" varchar(160) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "plan_id" TEXT NOT NULL REFERENCES "core_plan" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "refunded_cents" bigint unsigned NOT NULL CHECK ("refunded_cents" >= 0), CONSTRAINT "uniq_order_idempotency_per_tenant" UNIQUE ("tenant_id", "idempotency_key"));
INSERT INTO "new__core_order" ("id", "status", "amount_cents", "currency", "channel", "provider_trade_no", "paid_at", "idempotency_key", "created_at", "updated_at", "created_by_id", "plan_id", "tenant_id", "refunded_cents", "payment_instruction") SELECT "id", "status", "amount_cents", "currency", "channel", "provider_trade_no", "paid_at", "idempotency_key", "created_at", "updated_at", "created_by_id", "plan_id", "tenant_id", "refunded_cents", '{}' FROM "core_order";
DROP TABLE "core_order";
ALTER TABLE "new__core_order" RENAME TO "core_order";
CREATE UNIQUE INDEX "uniq_order_provider_trade_no" ON "core_order" ("channel", "provider_trade_no") WHERE "provider_trade_no" > '';
CREATE INDEX "core_order_created_by_id_1264a02d" ON "core_order" ("created_by_id");
CREATE INDEX "core_order_plan_id_ec2af184" ON "core_order" ("plan_id");
CREATE INDEX "core_order_tenant_id_8c26156f" ON "core_order" ("tenant_id");
-- 0052_session_activity
--
-- Create model SessionActivity
--
CREATE TABLE "core_sessionactivity" ("session_key" varchar(64) NOT NULL PRIMARY KEY, "created_at" TIMESTAMP NOT NULL, "last_activity" TIMESTAMP NOT NULL, "revoked_at" TIMESTAMP NULL, "user_id" integer NOT NULL REFERENCES "platform_users" ("id"));
CREATE INDEX "core_sessionactivity_last_activity_3daec96f" ON "core_sessionactivity" ("last_activity");
CREATE INDEX "core_sessionactivity_user_id_40c0ebeb" ON "core_sessionactivity" ("user_id");
-- 0053_provider_accounts
--
-- Add field auth_mode to modelprovider
--
CREATE TABLE "new__core_modelprovider" ("auth_mode" varchar(32) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "provider_type" varchar(32) NOT NULL, "base_url" varchar(500) NOT NULL, "credential_ref" varchar(160) NOT NULL, "config" text NOT NULL CHECK ((JSON_VALID("config") OR "config" IS NULL)), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NULL REFERENCES "core_tenant" ("id"), "deployment_region" varchar(32) NOT NULL, CONSTRAINT "uniq_model_provider_name_per_tenant" UNIQUE ("tenant_id", "name"));
INSERT INTO "new__core_modelprovider" ("id", "name", "provider_type", "base_url", "credential_ref", "config", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "deployment_region", "auth_mode") SELECT "id", "name", "provider_type", "base_url", "credential_ref", "config", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "deployment_region", 'tenant_secret' FROM "core_modelprovider";
DROP TABLE "core_modelprovider";
ALTER TABLE "new__core_modelprovider" RENAME TO "core_modelprovider";
CREATE INDEX "core_modelprovider_created_by_id_d65969a2" ON "core_modelprovider" ("created_by_id");
CREATE INDEX "core_modelprovider_tenant_id_7abf3c0d" ON "core_modelprovider" ("tenant_id");
--
-- Add field preset_key to modelprovider
--
CREATE TABLE "new__core_modelprovider" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "provider_type" varchar(32) NOT NULL, "base_url" varchar(500) NOT NULL, "credential_ref" varchar(160) NOT NULL, "config" text NOT NULL CHECK ((JSON_VALID("config") OR "config" IS NULL)), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NULL REFERENCES "core_tenant" ("id"), "deployment_region" varchar(32) NOT NULL, "auth_mode" varchar(32) NOT NULL, "preset_key" varchar(32) NOT NULL, CONSTRAINT "uniq_model_provider_name_per_tenant" UNIQUE ("tenant_id", "name"));
INSERT INTO "new__core_modelprovider" ("id", "name", "provider_type", "base_url", "credential_ref", "config", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "deployment_region", "auth_mode", "preset_key") SELECT "id", "name", "provider_type", "base_url", "credential_ref", "config", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "deployment_region", "auth_mode", '' FROM "core_modelprovider";
DROP TABLE "core_modelprovider";
ALTER TABLE "new__core_modelprovider" RENAME TO "core_modelprovider";
CREATE INDEX "core_modelprovider_created_by_id_d65969a2" ON "core_modelprovider" ("created_by_id");
CREATE INDEX "core_modelprovider_tenant_id_7abf3c0d" ON "core_modelprovider" ("tenant_id");
--
-- Create model ProviderAccountLink
--
CREATE TABLE "core_provideraccountlink" ("id" TEXT NOT NULL PRIMARY KEY, "status" varchar(16) NOT NULL, "account_label" varchar(120) NOT NULL, "credential_ref" varchar(160) NOT NULL, "linked_at" TIMESTAMP NOT NULL, "revoked_at" TIMESTAMP NULL, "updated_at" TIMESTAMP NOT NULL, "provider_id" TEXT NOT NULL REFERENCES "core_modelprovider" ("id"), "user_id" integer NOT NULL REFERENCES "platform_users" ("id"));
--
-- Create model ProviderAccountUsage
--
CREATE TABLE "core_provideraccountusage" ("id" TEXT NOT NULL PRIMARY KEY, "idempotency_key" varchar(160) NOT NULL, "status" varchar(16) NOT NULL, "reserved_tokens" bigint NOT NULL, "consumed_tokens" bigint NOT NULL, "expires_at" TIMESTAMP NOT NULL, "settled_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "provider_id" TEXT NOT NULL REFERENCES "core_modelprovider" ("id"), "user_id" integer NOT NULL REFERENCES "platform_users" ("id"), CONSTRAINT "uniq_provider_account_usage_key" UNIQUE ("user_id", "provider_id", "idempotency_key"));
CREATE UNIQUE INDEX "uniq_active_provider_account_link" ON "core_provideraccountlink" ("user_id", "provider_id") WHERE "status" = 'linked';
CREATE INDEX "core_provideraccountlink_provider_id_71df98e2" ON "core_provideraccountlink" ("provider_id");
CREATE INDEX "core_provideraccountlink_user_id_fc7b393a" ON "core_provideraccountlink" ("user_id");
CREATE INDEX "core_provid_user_id_54729e_idx" ON "core_provideraccountlink" ("user_id", "status");
CREATE INDEX "core_provideraccountusage_provider_id_98bcac26" ON "core_provideraccountusage" ("provider_id");
CREATE INDEX "core_provideraccountusage_user_id_f3758f37" ON "core_provideraccountusage" ("user_id");
CREATE INDEX "core_provid_user_id_433483_idx" ON "core_provideraccountusage" ("user_id", "provider_id", "settled_at");
-- 0054_agent_chat
--
-- Create model AgentChatThread
--
CREATE TABLE "core_agentchatthread" ("id" TEXT NOT NULL PRIMARY KEY, "title" varchar(200) NOT NULL, "archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "model_config_id" TEXT NULL REFERENCES "core_modelconfig" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create model AgentChatMessage
--
CREATE TABLE "core_agentchatmessage" ("id" TEXT NOT NULL PRIMARY KEY, "role" varchar(16) NOT NULL, "content" text NOT NULL, "context" text NOT NULL CHECK ((JSON_VALID("context") OR "context" IS NULL)), "usage" text NOT NULL CHECK ((JSON_VALID("usage") OR "usage" IS NULL)), "error_code" varchar(64) NOT NULL, "created_at" TIMESTAMP NOT NULL, "author_id" integer NULL REFERENCES "platform_users" ("id"), "thread_id" TEXT NOT NULL REFERENCES "core_agentchatthread" ("id"));
--
-- Create index chat_thread_scope_idx on field(s) tenant, project, -updated_at of model agentchatthread
--
CREATE INDEX "chat_thread_scope_idx" ON "core_agentchatthread" ("tenant_id", "project_id", "updated_at" DESC);
--
-- Create index chat_message_thread_idx on field(s) thread, created_at of model agentchatmessage
--
CREATE INDEX "chat_message_thread_idx" ON "core_agentchatmessage" ("thread_id", "created_at");
CREATE INDEX "core_agentchatthread_created_by_id_09438486" ON "core_agentchatthread" ("created_by_id");
CREATE INDEX "core_agentchatthread_model_config_id_7d1a2891" ON "core_agentchatthread" ("model_config_id");
CREATE INDEX "core_agentchatthread_project_id_a25708b9" ON "core_agentchatthread" ("project_id");
CREATE INDEX "core_agentchatthread_tenant_id_acbbd1bd" ON "core_agentchatthread" ("tenant_id");
CREATE INDEX "core_agentchatmessage_author_id_9558eac3" ON "core_agentchatmessage" ("author_id");
CREATE INDEX "core_agentchatmessage_thread_id_763604bf" ON "core_agentchatmessage" ("thread_id");
-- 0055_paymentchannelsecret
--
-- Create model PaymentChannelSecret
--
CREATE TABLE "core_paymentchannelsecret" ("id" TEXT NOT NULL PRIMARY KEY, "channel" varchar(32) NOT NULL, "slot" varchar(64) NOT NULL, "sealed_value" text NOT NULL, "updated_at" TIMESTAMP NOT NULL, "updated_by_id" integer NULL REFERENCES "platform_users" ("id"));
CREATE UNIQUE INDEX "core_paymentchannelsecret_channel_slot_6bec200e_uniq" ON "core_paymentchannelsecret" ("channel", "slot");
CREATE INDEX "core_paymentchannelsecret_updated_by_id_e6cd165d" ON "core_paymentchannelsecret" ("updated_by_id");
-- 0056_notificationendpoint
--
-- Create model NotificationEndpoint
--
CREATE TABLE "core_notificationendpoint" ("id" TEXT NOT NULL PRIMARY KEY, "channel" varchar(16) NOT NULL, "name" varchar(120) NOT NULL, "sealed_url" text NOT NULL, "sealed_secret" text NOT NULL, "enabled" bool NOT NULL, "last_sent_at" TIMESTAMP NULL, "last_error" varchar(200) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
CREATE UNIQUE INDEX "core_notificationendpoint_tenant_id_channel_name_78fae35e_uniq" ON "core_notificationendpoint" ("tenant_id", "channel", "name");
CREATE INDEX "core_notificationendpoint_tenant_id_2fce176b" ON "core_notificationendpoint" ("tenant_id");
-- 0057_membercredentialsecret
--
-- Create model MemberCredentialSecret
--
CREATE TABLE "core_membercredentialsecret" ("id" TEXT NOT NULL PRIMARY KEY, "sealed_value" text NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "user_id" integer NOT NULL REFERENCES "platform_users" ("id"));
CREATE INDEX "core_membercredentialsecret_user_id_532a1b24" ON "core_membercredentialsecret" ("user_id");
-- 0058_provideroauthstate
--
-- Create model ProviderOAuthState
--
CREATE TABLE "core_provideroauthstate" ("id" TEXT NOT NULL PRIMARY KEY, "preset_key" varchar(64) NOT NULL, "state" varchar(128) NOT NULL UNIQUE, "sealed_verifier" text NOT NULL, "redirect_uri" text NOT NULL, "expires_at" TIMESTAMP NOT NULL, "consumed_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "user_id" integer NOT NULL REFERENCES "platform_users" ("id"));
CREATE INDEX "core_provideroauthstate_tenant_id_ed2a32f3" ON "core_provideroauthstate" ("tenant_id");
CREATE INDEX "core_provideroauthstate_user_id_06e58b9e" ON "core_provideroauthstate" ("user_id");
-- 0059_refresh_preset_models
--
-- Raw Python operation
--
-- THIS OPERATION CANNOT BE WRITTEN AS SQL
-- 0060_modelprovider_platform_pool
--
-- Alter field auth_mode on modelprovider
--
-- (no-op)
-- 0061_registryimage_registryevent_and_more
--
-- Create model RegistryImage
--
CREATE TABLE "core_registryimage" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(200) NOT NULL, "category" varchar(20) NOT NULL, "source_repository" varchar(200) NOT NULL, "build_ref" varchar(120) NOT NULL, "digest" varchar(80) NOT NULL, "size_bytes" bigint NOT NULL, "status" varchar(16) NOT NULL, "stage" varchar(16) NOT NULL, "scan_status" varchar(16) NOT NULL, "scan_findings" integer unsigned NOT NULL CHECK ("scan_findings" >= 0), "sbom_ref" varchar(200) NOT NULL, "signed" bool NOT NULL, "promoted_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
--
-- Create model RegistryEvent
--
CREATE TABLE "core_registryevent" ("id" TEXT NOT NULL PRIMARY KEY, "action" varchar(16) NOT NULL, "detail" text NOT NULL CHECK ((JSON_VALID("detail") OR "detail" IS NULL)), "created_at" TIMESTAMP NOT NULL, "actor_id" integer NULL REFERENCES "platform_users" ("id"), "image_id" TEXT NOT NULL REFERENCES "core_registryimage" ("id"));
--
-- Create constraint uniq_registry_image_digest_per_project on model registryimage
--
CREATE TABLE "new__core_registryimage" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(200) NOT NULL, "category" varchar(20) NOT NULL, "source_repository" varchar(200) NOT NULL, "build_ref" varchar(120) NOT NULL, "digest" varchar(80) NOT NULL, "size_bytes" bigint NOT NULL, "status" varchar(16) NOT NULL, "stage" varchar(16) NOT NULL, "scan_status" varchar(16) NOT NULL, "scan_findings" integer unsigned NOT NULL CHECK ("scan_findings" >= 0), "sbom_ref" varchar(200) NOT NULL, "signed" bool NOT NULL, "promoted_at" TIMESTAMP NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_registry_image_digest_per_project" UNIQUE ("project_id", "name", "digest"));
INSERT INTO "new__core_registryimage" ("id", "name", "category", "source_repository", "build_ref", "digest", "size_bytes", "status", "stage", "scan_status", "scan_findings", "sbom_ref", "signed", "promoted_at", "created_at", "updated_at", "created_by_id", "project_id", "tenant_id") SELECT "id", "name", "category", "source_repository", "build_ref", "digest", "size_bytes", "status", "stage", "scan_status", "scan_findings", "sbom_ref", "signed", "promoted_at", "created_at", "updated_at", "created_by_id", "project_id", "tenant_id" FROM "core_registryimage";
DROP TABLE "core_registryimage";
ALTER TABLE "new__core_registryimage" RENAME TO "core_registryimage";
CREATE INDEX "core_registryevent_actor_id_c558c265" ON "core_registryevent" ("actor_id");
CREATE INDEX "core_registryevent_image_id_b9de0d9c" ON "core_registryevent" ("image_id");
CREATE INDEX "core_registryimage_created_by_id_a0df498b" ON "core_registryimage" ("created_by_id");
CREATE INDEX "core_registryimage_project_id_f3a49df0" ON "core_registryimage" ("project_id");
CREATE INDEX "core_registryimage_tenant_id_425d4a3c" ON "core_registryimage" ("tenant_id");
-- 0062_agentchatthread_effort_and_more
--
-- Add field effort to agentchatthread
--
CREATE TABLE "new__core_agentchatthread" ("effort" varchar(16) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "title" varchar(200) NOT NULL, "archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "model_config_id" TEXT NULL REFERENCES "core_modelconfig" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
INSERT INTO "new__core_agentchatthread" ("id", "title", "archived", "created_at", "updated_at", "created_by_id", "model_config_id", "project_id", "tenant_id", "effort") SELECT "id", "title", "archived", "created_at", "updated_at", "created_by_id", "model_config_id", "project_id", "tenant_id", 'auto' FROM "core_agentchatthread";
DROP TABLE "core_agentchatthread";
ALTER TABLE "new__core_agentchatthread" RENAME TO "core_agentchatthread";
CREATE INDEX "core_agentchatthread_created_by_id_09438486" ON "core_agentchatthread" ("created_by_id");
CREATE INDEX "core_agentchatthread_model_config_id_7d1a2891" ON "core_agentchatthread" ("model_config_id");
CREATE INDEX "core_agentchatthread_project_id_a25708b9" ON "core_agentchatthread" ("project_id");
CREATE INDEX "core_agentchatthread_tenant_id_acbbd1bd" ON "core_agentchatthread" ("tenant_id");
CREATE INDEX "chat_thread_scope_idx" ON "core_agentchatthread" ("tenant_id", "project_id", "updated_at" DESC);
--
-- Add field execution_target to agentchatthread
--
CREATE TABLE "new__core_agentchatthread" ("id" TEXT NOT NULL PRIMARY KEY, "title" varchar(200) NOT NULL, "archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "model_config_id" TEXT NULL REFERENCES "core_modelconfig" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "effort" varchar(16) NOT NULL, "execution_target" varchar(16) NOT NULL);
INSERT INTO "new__core_agentchatthread" ("id", "title", "archived", "created_at", "updated_at", "created_by_id", "model_config_id", "project_id", "tenant_id", "effort", "execution_target") SELECT "id", "title", "archived", "created_at", "updated_at", "created_by_id", "model_config_id", "project_id", "tenant_id", "effort", 'cloud' FROM "core_agentchatthread";
DROP TABLE "core_agentchatthread";
ALTER TABLE "new__core_agentchatthread" RENAME TO "core_agentchatthread";
CREATE INDEX "core_agentchatthread_created_by_id_09438486" ON "core_agentchatthread" ("created_by_id");
CREATE INDEX "core_agentchatthread_model_config_id_7d1a2891" ON "core_agentchatthread" ("model_config_id");
CREATE INDEX "core_agentchatthread_project_id_a25708b9" ON "core_agentchatthread" ("project_id");
CREATE INDEX "core_agentchatthread_tenant_id_acbbd1bd" ON "core_agentchatthread" ("tenant_id");
CREATE INDEX "chat_thread_scope_idx" ON "core_agentchatthread" ("tenant_id", "project_id", "updated_at" DESC);
--
-- Add field goal to agentchatthread
--
CREATE TABLE "new__core_agentchatthread" ("id" TEXT NOT NULL PRIMARY KEY, "title" varchar(200) NOT NULL, "archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "model_config_id" TEXT NULL REFERENCES "core_modelconfig" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "effort" varchar(16) NOT NULL, "execution_target" varchar(16) NOT NULL, "goal" varchar(500) NOT NULL);
INSERT INTO "new__core_agentchatthread" ("id", "title", "archived", "created_at", "updated_at", "created_by_id", "model_config_id", "project_id", "tenant_id", "effort", "execution_target", "goal") SELECT "id", "title", "archived", "created_at", "updated_at", "created_by_id", "model_config_id", "project_id", "tenant_id", "effort", "execution_target", '' FROM "core_agentchatthread";
DROP TABLE "core_agentchatthread";
ALTER TABLE "new__core_agentchatthread" RENAME TO "core_agentchatthread";
CREATE INDEX "core_agentchatthread_created_by_id_09438486" ON "core_agentchatthread" ("created_by_id");
CREATE INDEX "core_agentchatthread_model_config_id_7d1a2891" ON "core_agentchatthread" ("model_config_id");
CREATE INDEX "core_agentchatthread_project_id_a25708b9" ON "core_agentchatthread" ("project_id");
CREATE INDEX "core_agentchatthread_tenant_id_acbbd1bd" ON "core_agentchatthread" ("tenant_id");
CREATE INDEX "chat_thread_scope_idx" ON "core_agentchatthread" ("tenant_id", "project_id", "updated_at" DESC);
--
-- Add field plan_first to agentchatthread
--
CREATE TABLE "new__core_agentchatthread" ("id" TEXT NOT NULL PRIMARY KEY, "title" varchar(200) NOT NULL, "archived" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "model_config_id" TEXT NULL REFERENCES "core_modelconfig" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "effort" varchar(16) NOT NULL, "execution_target" varchar(16) NOT NULL, "goal" varchar(500) NOT NULL, "plan_first" bool NOT NULL);
INSERT INTO "new__core_agentchatthread" ("id", "title", "archived", "created_at", "updated_at", "created_by_id", "model_config_id", "project_id", "tenant_id", "effort", "execution_target", "goal", "plan_first") SELECT "id", "title", "archived", "created_at", "updated_at", "created_by_id", "model_config_id", "project_id", "tenant_id", "effort", "execution_target", "goal", 0 FROM "core_agentchatthread";
DROP TABLE "core_agentchatthread";
ALTER TABLE "new__core_agentchatthread" RENAME TO "core_agentchatthread";
CREATE INDEX "core_agentchatthread_created_by_id_09438486" ON "core_agentchatthread" ("created_by_id");
CREATE INDEX "core_agentchatthread_model_config_id_7d1a2891" ON "core_agentchatthread" ("model_config_id");
CREATE INDEX "core_agentchatthread_project_id_a25708b9" ON "core_agentchatthread" ("project_id");
CREATE INDEX "core_agentchatthread_tenant_id_acbbd1bd" ON "core_agentchatthread" ("tenant_id");
CREATE INDEX "chat_thread_scope_idx" ON "core_agentchatthread" ("tenant_id", "project_id", "updated_at" DESC);
--
-- Create model AgentChatAttachment
--
CREATE TABLE "core_agentchatattachment" ("id" TEXT NOT NULL PRIMARY KEY, "filename" varchar(255) NOT NULL, "content_type" varchar(128) NOT NULL, "size_bytes" integer unsigned NOT NULL CHECK ("size_bytes" >= 0), "file" varchar(100) NOT NULL, "created_at" TIMESTAMP NOT NULL, "thread_id" TEXT NOT NULL REFERENCES "core_agentchatthread" ("id"), "uploaded_by_id" integer NOT NULL REFERENCES "platform_users" ("id"));
--
-- Create model ScheduledChatJob
--
CREATE TABLE "core_scheduledchatjob" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(160) NOT NULL, "prompt" text NOT NULL, "cadence" varchar(16) NOT NULL, "hour" smallint unsigned NOT NULL CHECK ("hour" >= 0), "weekday" smallint unsigned NOT NULL CHECK ("weekday" >= 0), "execution_target" varchar(16) NOT NULL, "effort" varchar(16) NOT NULL, "status" varchar(16) NOT NULL, "next_run_at" TIMESTAMP NOT NULL, "last_run_at" TIMESTAMP NULL, "last_error_code" varchar(64) NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "model_config_id" TEXT NULL REFERENCES "core_modelconfig" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "thread_id" TEXT NULL REFERENCES "core_agentchatthread" ("id"));
CREATE INDEX "core_agentchatattachment_thread_id_175f7be9" ON "core_agentchatattachment" ("thread_id");
CREATE INDEX "core_agentchatattachment_uploaded_by_id_90b54fa3" ON "core_agentchatattachment" ("uploaded_by_id");
CREATE INDEX "chat_attachment_thread_idx" ON "core_agentchatattachment" ("thread_id", "created_at");
CREATE INDEX "core_scheduledchatjob_created_by_id_02b20673" ON "core_scheduledchatjob" ("created_by_id");
CREATE INDEX "core_scheduledchatjob_model_config_id_92c723cd" ON "core_scheduledchatjob" ("model_config_id");
CREATE INDEX "core_scheduledchatjob_project_id_bb457123" ON "core_scheduledchatjob" ("project_id");
CREATE INDEX "core_scheduledchatjob_tenant_id_dad517c6" ON "core_scheduledchatjob" ("tenant_id");
CREATE INDEX "core_scheduledchatjob_thread_id_026f7653" ON "core_scheduledchatjob" ("thread_id");
CREATE INDEX "sched_job_due_idx" ON "core_scheduledchatjob" ("status", "next_run_at");
CREATE INDEX "sched_job_scope_idx" ON "core_scheduledchatjob" ("tenant_id", "project_id");
-- 0063_agentchatmessage_tool_calls
--
-- Add field tool_calls to agentchatmessage
--
CREATE TABLE "new__core_agentchatmessage" ("tool_calls" text NOT NULL CHECK ((JSON_VALID("tool_calls") OR "tool_calls" IS NULL)), "id" TEXT NOT NULL PRIMARY KEY, "role" varchar(16) NOT NULL, "content" text NOT NULL, "context" text NOT NULL CHECK ((JSON_VALID("context") OR "context" IS NULL)), "usage" text NOT NULL CHECK ((JSON_VALID("usage") OR "usage" IS NULL)), "error_code" varchar(64) NOT NULL, "created_at" TIMESTAMP NOT NULL, "author_id" integer NULL REFERENCES "platform_users" ("id"), "thread_id" TEXT NOT NULL REFERENCES "core_agentchatthread" ("id"));
INSERT INTO "new__core_agentchatmessage" ("id", "role", "content", "context", "usage", "error_code", "created_at", "author_id", "thread_id", "tool_calls") SELECT "id", "role", "content", "context", "usage", "error_code", "created_at", "author_id", "thread_id", '[]' FROM "core_agentchatmessage";
DROP TABLE "core_agentchatmessage";
ALTER TABLE "new__core_agentchatmessage" RENAME TO "core_agentchatmessage";
CREATE INDEX "core_agentchatmessage_author_id_9558eac3" ON "core_agentchatmessage" ("author_id");
CREATE INDEX "core_agentchatmessage_thread_id_763604bf" ON "core_agentchatmessage" ("thread_id");
CREATE INDEX "chat_message_thread_idx" ON "core_agentchatmessage" ("thread_id", "created_at");
-- 0064_alter_agentnode_node_type
--
-- Alter field node_type on agentnode
--
-- (no-op)
-- 0065_alter_agentplugindefinition_protocol
--
-- Alter field protocol on agentplugindefinition
--
CREATE TABLE "new__core_agentplugindefinition" ("protocol" varchar(32) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "plugin_key" varchar(100) NOT NULL, "name" varchar(120) NOT NULL, "description" text NOT NULL, "version" varchar(40) NOT NULL, "endpoint_url" varchar(500) NOT NULL, "auth_required" bool NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "input_schema" text NOT NULL CHECK ((JSON_VALID("input_schema") OR "input_schema" IS NULL)), "output_schema" text NOT NULL CHECK ((JSON_VALID("output_schema") OR "output_schema" IS NULL)), "timeout_seconds" smallint unsigned NOT NULL CHECK ("timeout_seconds" >= 0), "max_concurrency" smallint unsigned NOT NULL CHECK ("max_concurrency" >= 0), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "manifest" text NOT NULL CHECK ((JSON_VALID("manifest") OR "manifest" IS NULL)), CONSTRAINT "uniq_agent_plugin_key_version" UNIQUE ("tenant_id", "plugin_key", "version"));
INSERT INTO "new__core_agentplugindefinition" ("id", "plugin_key", "name", "description", "version", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest", "protocol") SELECT "id", "plugin_key", "name", "description", "version", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest", "protocol" FROM "core_agentplugindefinition";
DROP TABLE "core_agentplugindefinition";
ALTER TABLE "new__core_agentplugindefinition" RENAME TO "core_agentplugindefinition";
CREATE INDEX "core_agentplugindefinition_plugin_key_9f6e4684" ON "core_agentplugindefinition" ("plugin_key");
CREATE INDEX "core_agentplugindefinition_created_by_id_875582da" ON "core_agentplugindefinition" ("created_by_id");
CREATE INDEX "core_agentplugindefinition_tenant_id_e1663a50" ON "core_agentplugindefinition" ("tenant_id");
-- 0066_delete_legacy_connector_stack
--
-- Delete model ToolApprovalRequest
--
DROP TABLE "core_toolapprovalrequest";
--
-- Delete model PluginConnection
--
DROP TABLE "core_pluginconnection";
--
-- Delete model PluginTool
--
DROP TABLE "core_plugintool";
--
-- Delete model PluginInstallation
--
DROP TABLE "core_plugininstallation";
--
-- Delete model PluginDefinition
--
DROP TABLE "core_plugindefinition";
-- 0067_align_agent_plugin_protocol
--
-- Alter field protocol on agentplugindefinition
--
CREATE TABLE "new__core_agentplugindefinition" ("protocol" varchar(32) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "plugin_key" varchar(100) NOT NULL, "name" varchar(120) NOT NULL, "description" text NOT NULL, "version" varchar(40) NOT NULL, "endpoint_url" varchar(500) NOT NULL, "auth_required" bool NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "input_schema" text NOT NULL CHECK ((JSON_VALID("input_schema") OR "input_schema" IS NULL)), "output_schema" text NOT NULL CHECK ((JSON_VALID("output_schema") OR "output_schema" IS NULL)), "timeout_seconds" smallint unsigned NOT NULL CHECK ("timeout_seconds" >= 0), "max_concurrency" smallint unsigned NOT NULL CHECK ("max_concurrency" >= 0), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "manifest" text NOT NULL CHECK ((JSON_VALID("manifest") OR "manifest" IS NULL)), CONSTRAINT "uniq_agent_plugin_key_version" UNIQUE ("tenant_id", "plugin_key", "version"));
INSERT INTO "new__core_agentplugindefinition" ("id", "plugin_key", "name", "description", "version", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest", "protocol") SELECT "id", "plugin_key", "name", "description", "version", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest", "protocol" FROM "core_agentplugindefinition";
DROP TABLE "core_agentplugindefinition";
ALTER TABLE "new__core_agentplugindefinition" RENAME TO "core_agentplugindefinition";
CREATE INDEX "core_agentplugindefinition_plugin_key_9f6e4684" ON "core_agentplugindefinition" ("plugin_key");
CREATE INDEX "core_agentplugindefinition_created_by_id_875582da" ON "core_agentplugindefinition" ("created_by_id");
CREATE INDEX "core_agentplugindefinition_tenant_id_e1663a50" ON "core_agentplugindefinition" ("tenant_id");
-- 0068_agent_plugin_credential_vault
--
-- Add field credential_ref to agentplugindefinition
--
CREATE TABLE "new__core_agentplugindefinition" ("credential_ref" varchar(2048) NOT NULL, "id" TEXT NOT NULL PRIMARY KEY, "plugin_key" varchar(100) NOT NULL, "name" varchar(120) NOT NULL, "description" text NOT NULL, "version" varchar(40) NOT NULL, "protocol" varchar(32) NOT NULL, "endpoint_url" varchar(500) NOT NULL, "auth_required" bool NOT NULL, "capabilities" text NOT NULL CHECK ((JSON_VALID("capabilities") OR "capabilities" IS NULL)), "input_schema" text NOT NULL CHECK ((JSON_VALID("input_schema") OR "input_schema" IS NULL)), "output_schema" text NOT NULL CHECK ((JSON_VALID("output_schema") OR "output_schema" IS NULL)), "timeout_seconds" smallint unsigned NOT NULL CHECK ("timeout_seconds" >= 0), "max_concurrency" smallint unsigned NOT NULL CHECK ("max_concurrency" >= 0), "enabled" bool NOT NULL, "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), "manifest" text NOT NULL CHECK ((JSON_VALID("manifest") OR "manifest" IS NULL)), CONSTRAINT "uniq_agent_plugin_key_version" UNIQUE ("tenant_id", "plugin_key", "version"));
INSERT INTO "new__core_agentplugindefinition" ("id", "plugin_key", "name", "description", "version", "protocol", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest", "credential_ref") SELECT "id", "plugin_key", "name", "description", "version", "protocol", "endpoint_url", "auth_required", "capabilities", "input_schema", "output_schema", "timeout_seconds", "max_concurrency", "enabled", "created_at", "updated_at", "created_by_id", "tenant_id", "manifest", '' FROM "core_agentplugindefinition";
DROP TABLE "core_agentplugindefinition";
ALTER TABLE "new__core_agentplugindefinition" RENAME TO "core_agentplugindefinition";
CREATE INDEX "core_agentplugindefinition_plugin_key_9f6e4684" ON "core_agentplugindefinition" ("plugin_key");
CREATE INDEX "core_agentplugindefinition_created_by_id_875582da" ON "core_agentplugindefinition" ("created_by_id");
CREATE INDEX "core_agentplugindefinition_tenant_id_e1663a50" ON "core_agentplugindefinition" ("tenant_id");
--
-- Create model AgentPluginCredential
--
CREATE TABLE "core_agentplugincredential" ("id" TEXT NOT NULL PRIMARY KEY, "name" varchar(120) NOT NULL, "sealed_value" text NOT NULL, "created_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"));
CREATE INDEX "core_agentplugincredential_created_by_id_bdddeda8" ON "core_agentplugincredential" ("created_by_id");
CREATE INDEX "core_agentplugincredential_tenant_id_7a69ce8d" ON "core_agentplugincredential" ("tenant_id");
-- 0069_cloud_vm_instances
--
-- Create model CloudVmInstance
--
CREATE TABLE "core_cloudvminstance" ("id" TEXT NOT NULL PRIMARY KEY, "provider_key" varchar(64) NOT NULL, "server_id" varchar(128) NOT NULL, "name" varchar(128) NOT NULL, "flavor_ref" varchar(128) NOT NULL, "image_ref" varchar(128) NOT NULL, "network_ids" text NOT NULL CHECK ((JSON_VALID("network_ids") OR "network_ids" IS NULL)), "status" varchar(32) NOT NULL, "ip_address" TEXT NULL, "provider_metadata" text NOT NULL CHECK ((JSON_VALID("provider_metadata") OR "provider_metadata" IS NULL)), "created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, "created_by_id" integer NOT NULL REFERENCES "platform_users" ("id"), "project_id" TEXT NOT NULL REFERENCES "core_project" ("id"), "tenant_id" TEXT NOT NULL REFERENCES "core_tenant" ("id"), CONSTRAINT "uniq_cloud_vm_server" UNIQUE ("tenant_id", "project_id", "provider_key", "server_id"));
CREATE INDEX "core_cloudvminstance_created_by_id_885c6339" ON "core_cloudvminstance" ("created_by_id");
CREATE INDEX "core_cloudvminstance_project_id_e26f485a" ON "core_cloudvminstance" ("project_id");
CREATE INDEX "core_cloudvminstance_tenant_id_c3879d71" ON "core_cloudvminstance" ("tenant_id");
CREATE INDEX "cloud_vm_scope_idx" ON "core_cloudvminstance" ("tenant_id", "project_id", "provider_key");
-- 0070_modelconfig_fallback_model
--
-- Add field fallback_model to modelconfig
--
ALTER TABLE "core_modelconfig" ADD COLUMN "fallback_model_id" TEXT NULL REFERENCES "core_modelconfig" ("id");
CREATE INDEX "core_modelconfig_fallback_model_id_ab570f98" ON "core_modelconfig" ("fallback_model_id");
