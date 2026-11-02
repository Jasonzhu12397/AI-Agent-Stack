# ADR-0002: Tenant and project authorization baseline

- Status: Accepted for the first P1-1 vertical slice.
- Date: 2026-11-02

## Identity source

Django's configured authentication backends establish the authenticated user. API views require an authenticated Principal by default. No endpoint accepts a client-supplied user identity as authority.

## Resource model

- Tenant: top-level organization boundary.
- TenantMembership: unique user-to-tenant relationship with owner, admin, member or viewer role.
- Project: belongs to exactly one tenant.
- ProjectMembership: unique user-to-project relationship with admin, editor or viewer role.

Project visibility requires both tenant membership and project membership. Tenant membership alone does not implicitly expose every project. This explicit rule avoids accidental privilege expansion; a later role policy may grant tenant owner/admin inherited access only through a separately tested decision.

## API slice

- `GET /api/v1/tenants/`: list only tenants associated with the authenticated user.
- `GET /api/v1/tenants/{tenant_id}/projects/`: first resolve tenant through the user's membership, then return only projects with an explicit project membership.
- Unauthorized cross-tenant lookups return 404 to avoid disclosing resource existence.
- Pagination is applied after authorization filtering; counts and pages must not include other tenants' records.

No public tenant registration or membership mutation API is introduced in this slice. Membership provisioning remains an administrative operation until a separately authorized workflow is designed.

## Required negative tests

Unauthenticated access, cross-tenant lookup, client tenant_id spoofing, project membership isolation, immediate membership revocation and pagination count leakage.
