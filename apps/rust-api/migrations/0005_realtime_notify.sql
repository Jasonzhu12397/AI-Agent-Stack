-- Realtime wakeups for the project event stream.
--
-- The Rust API holds one LISTEN connection per SSE client; writing the row is
-- enough to wake it because the notification is emitted by the database itself.
-- A payload that fails to notify never loses an event: the consumer re-queries
-- from its cursor after every wakeup, so the notification is an optimization.

CREATE OR REPLACE FUNCTION platform_events_notify() RETURNS trigger AS $$
DECLARE
    scope_payload text;
BEGIN
    IF NEW.tenant_id IS NULL OR NEW.project_id IS NULL THEN
        RETURN NEW;
    END IF;
    scope_payload := json_build_object(
        'tenant_id', NEW.tenant_id,
        'project_id', NEW.project_id,
        'kind', TG_ARGV[0],
        'id', NEW.id,
        'at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
    )::text;
    PERFORM pg_notify('platform_events', scope_payload);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS platform_events_task ON core_task;
CREATE TRIGGER platform_events_task
    AFTER INSERT OR UPDATE ON core_task
    FOR EACH ROW EXECUTE FUNCTION platform_events_notify('task.updated');

DROP TRIGGER IF EXISTS platform_events_audit ON core_auditevent;
CREATE TRIGGER platform_events_audit
    AFTER INSERT ON core_auditevent
    FOR EACH ROW EXECUTE FUNCTION platform_events_notify('audit.created');
