from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0064_alter_agentnode_node_type")]

    operations = [
        migrations.AlterField(
            model_name="agentplugindefinition",
            name="protocol",
            field=models.CharField(
                choices=[
                    ("http_json", "HTTP JSON"),
                    ("mcp", "Model Context Protocol"),
                    ("a2a", "Agent-to-Agent"),
                    ("openai_compatible", "OpenAI Compatible"),
                    ("open_data", "Open Data (built-in)"),
                ],
                default="http_json",
                max_length=32,
            ),
        )
    ]
