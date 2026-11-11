from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('phase4_ops', '0001_initial')]

    operations = [
        migrations.AddField(
            model_name='pipelinetask',
            name='registration',
            field=models.JSONField(null=True),
        ),
    ]
