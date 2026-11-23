from django.db import migrations


def backfill_command_deployment(apps, schema_editor):
    """Bind legacy unattributed commands to the deployment their node serves.

    Before command creation recorded the deployment, rollback and upgrade
    commands were stored without one. Retention protection and the post-change
    backup trigger both read the deployment link, so these rows could never
    protect the snapshot they named. Resolve the same node-to-deployment chain
    the command view uses, once, here. The pass is idempotent: only rows with
    no deployment are touched, and re-running it changes nothing.
    """

    Command = apps.get_model("core", "AgentCommand")
    Deployment = apps.get_model("core", "ClusterDeployment")
    database = schema_editor.connection.alias if schema_editor else "default"

    commands = (
        Command.objects.using(database)
        .filter(deployment__isnull=True)
        .select_related("node")
        .iterator()
    )
    for command in commands:
        node = command.node
        if node is None:
            continue
        deployment = (
            Deployment.objects.using(database)
            .filter(
                project_id=node.project_id,
                cluster_name=node.cluster_name,
                status="succeeded",
            )
            .order_by("-updated_at")
            .first()
        )
        if deployment is not None:
            command.deployment_id = deployment.id
            command.save(update_fields=["deployment"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0032_agent_machine_inventory_revision"),
    ]

    operations = [
        migrations.RunPython(
            backfill_command_deployment,
            migrations.RunPython.noop,
        ),
    ]
