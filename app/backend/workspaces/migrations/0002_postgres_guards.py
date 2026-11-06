from django.db import migrations

FORWARD = r"""
CREATE FUNCTION platform_owner_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid; state text; owners integer;
BEGIN
  IF TG_TABLE_NAME = 'workspaces_workspace' THEN
    target := CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
  ELSE
    target := CASE WHEN TG_OP='DELETE' THEN OLD.workspace_id ELSE NEW.workspace_id END;
  END IF;
  SELECT status INTO state FROM workspaces_workspace WHERE id=target FOR UPDATE;
  IF state IN ('ACTIVE','SUSPENDED') THEN
    SELECT count(*) INTO owners FROM workspaces_workspacemembership
      WHERE workspace_id=target AND status='ACTIVE' AND role='OWNER';
    IF owners <> 1 THEN
      RAISE EXCEPTION 'active workspace requires exactly one owner' USING ERRCODE='23514';
    END IF;
  END IF;
  RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER platform_workspace_owner_guard
 AFTER INSERT OR UPDATE ON workspaces_workspace DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION platform_owner_guard();
CREATE CONSTRAINT TRIGGER platform_membership_owner_guard
 AFTER INSERT OR UPDATE OR DELETE ON workspaces_workspacemembership DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION platform_owner_guard();
CREATE UNIQUE INDEX platform_member_workspace_key ON workspaces_workspacemembership(id,workspace_id);
ALTER TABLE workspaces_workspaceinvitation ADD CONSTRAINT platform_inviter_workspace
 FOREIGN KEY (inviter_membership_id,workspace_id)
 REFERENCES workspaces_workspacemembership(id,workspace_id) DEFERRABLE INITIALLY IMMEDIATE;
CREATE FUNCTION platform_audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='UPDATE' AND NEW.actor_id IS NULL AND
     (to_jsonb(NEW)-'actor_id') IS NOT DISTINCT FROM (to_jsonb(OLD)-'actor_id') THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'authorization audit is append only' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER platform_audit_immutable BEFORE UPDATE OR DELETE ON workspaces_authorizationaudit
 FOR EACH ROW EXECUTE FUNCTION platform_audit_guard();
"""

REVERSE = r"""
DROP TRIGGER platform_audit_immutable ON workspaces_authorizationaudit;
DROP FUNCTION platform_audit_guard();
ALTER TABLE workspaces_workspaceinvitation DROP CONSTRAINT platform_inviter_workspace;
DROP INDEX platform_member_workspace_key;
DROP TRIGGER platform_membership_owner_guard ON workspaces_workspacemembership;
DROP TRIGGER platform_workspace_owner_guard ON workspaces_workspace;
DROP FUNCTION platform_owner_guard();
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FORWARD)


def remove(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(REVERSE)


class Migration(migrations.Migration):
    dependencies = [('workspaces','0001_initial')]
    operations = [migrations.RunPython(install,remove)]
