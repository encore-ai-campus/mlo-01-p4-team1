import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('phase4_ops', '0002_pipelinetask_registration')]

    operations = [
        migrations.AddField(
            model_name='pipelinetask',
            name='is_current',
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name='pipelinetask',
            name='item',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    related_name='tasks', to='phase4_ops.songitem'),
        ),
        migrations.AddConstraint(
            model_name='pipelinetask',
            constraint=models.UniqueConstraint(fields=('item',), condition=models.Q(is_current=True),
                                               name='p4_current_task'),
        ),
    ]
