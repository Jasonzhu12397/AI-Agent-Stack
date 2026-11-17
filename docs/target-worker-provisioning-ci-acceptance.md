# Target Worker Provisioning CI Acceptance

The Target Worker executor is validated by the repository CI workflow on a `refactor/**` push.

The Python job must pass Ruff, Django system checks, migration drift checks, the complete pytest suite, and deployment shell syntax checks. The tests include worker provisioning, Cloud Control action authorization, and namespace-scoped deployment RBAC.

This CI run is unit/integration validation only. It does not substitute for running the selected Cluster API, CABPK, Metal3 provider and Ironic versions against physical BMC-backed hosts. Those checks remain a separate hardware acceptance gate.
