path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/projects/{{identity.entity.metadata.project_id}}/bmc/*" {
  capabilities = ["read"]
}

path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/projects/{{identity.entity.metadata.project_id}}/redfish/*" {
  capabilities = ["read"]
}
