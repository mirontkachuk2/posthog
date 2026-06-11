from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("warehouse_sources", "0008_alter_pendingsourcecredential_source_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="externaldataschema",
            name="s3_folder_path",
            field=models.CharField(blank=True, max_length=400, null=True),
        ),
    ]
