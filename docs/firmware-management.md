# Redfish Firmware Management

AI Agent Stack exposes BMC firmware inventory and firmware updates through the
Django platform API and the Metal3 Fleet workspace. Every call uses the machine's
configured BMC endpoint and credential reference; no BMC password, token or
inline secret is stored in the database or returned to the browser.

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/tenants/{tenant_id}/projects/{project_id}/fleet/machines/{machine_id}/firmware/` | Read `UpdateService`, `FirmwareInventory` and the latest 20 update jobs |
| `POST` | `.../firmware/upgrades/` | Submit a Redfish `UpdateService.SimpleUpdate` action |
| `GET` | `.../firmware/upgrades/{job_id}/` | Refresh the Redfish task monitor and persist its state |

`POST` requires:

- tenant administrator or project `ADMIN` membership;
- `confirm: true` in the JSON body;
- an `Idempotency-Key` header with 8–128 safe characters;
- `image_url` using HTTPS, without embedded credentials or a fragment;
- optional `apply_time` of `Immediate`, `OnReset` or `AtMaintenanceWindowStart`;
- optional `targets`, at most eight `/redfish/v1/...` component paths.

The response is `202` on first submission and `200` when the idempotency key
replays an existing job. A successful submission records the
`fleet.machine.firmware_update_requested` audit event. The image URL is stored
without its query string so signed URL parameters do not enter audit data.

## Security boundary

- Only literal private BMC IP addresses are accepted; loopback, link-local,
  multicast, reserved and unspecified addresses are rejected to prevent DNS
  rebinding.
- Redirects are disabled and TLS verification uses the configured trust bundle.
- Request and response bodies are bounded; the BMC response limit is 1 MiB.
- Redfish `SimpleUpdate` targets and task monitors must stay under the same
  `https://<bmc>/redfish/v1/` origin. The monitor URL is revalidated before every
  poll.
- Firmware submission is a destructive hardware operation. It is available only
  to tenant/project administrators and always requires explicit confirmation.
  EPH and Target Agents never auto-approve firmware work; the control plane is
  the only caller.
- The platform never proxies firmware binaries. The BMC pulls the image from the
  HTTPS URL supplied by the operator, so the BMC network must be able to reach
  that host.

## Persistence

`FirmwareUpdateJob` stores machine, tenant, project, requester, sanitized image
URL, component filter, apply time, targets, status, task monitor, Redfish task
state, message and timestamps. The unique constraint on
`(tenant, machine, idempotency_key)` makes retries safe. Migration:
`core/migrations/0030_firmware_update_job.py`.

## UI

Metal3 Fleet → Fleet Inventory → row action **Firmware** opens
**BMC Firmware Management**. The dialog shows UpdateService state, firmware
components and versions, a `SimpleUpdate` form, and recent jobs with a manual
task refresh. The dialog is available only for machines that have a persisted
hardware record and configured BMC endpoint/credential reference.

## Operational sequence

1. Configure the BMC endpoint, credential reference and trust bundle reference on
   the machine (`bmc-config` API or Metal3 Fleet configuration).
2. Open the firmware dialog and confirm the current component versions.
3. Submit the vendor image over HTTPS, choosing an apply time appropriate for the
   maintenance window.
4. Refresh the job until the Redfish task reaches `Completed` or a terminal
   failure state; correlate the job with the audit event and BMC task log.
5. Re-read the firmware inventory to verify the new version.

For physical acceptance evidence, keep the firmware job ID, BMC task state and
post-upgrade inventory snapshot with the Metal3 release manifest.
