from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('music', '0003_musicimportrun'), ('common', '0005_musicsource_archive')]

    operations = [
        migrations.CreateModel(
            name='TaggedMusicCandidate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('audio_sha256', models.CharField(max_length=64)),
                ('video_id', models.CharField(max_length=11)),
                ('record', models.JSONField()),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='common.musicsource')),
                ('track', models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT, to='music.track')),
            ],
            options={'constraints': [
                models.UniqueConstraint(fields=('source', 'audio_sha256'), name='tagged_music_source_audio'),
                models.UniqueConstraint(fields=('source', 'video_id'), name='tagged_music_source_video'),
            ]},
        ),
    ]
