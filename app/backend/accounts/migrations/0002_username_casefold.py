from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('accounts', '0001_initial'), ('auth', '0012_alter_user_first_name_max_length')]
    # Fails safely if an existing database contains conflicting administrator IDs.
    # Resolve those accounts explicitly before applying; never silently merge users.
    operations = [migrations.RunSQL(
        'CREATE UNIQUE INDEX accounts_username_ci ON auth_user (lower(username))',
        'DROP INDEX accounts_username_ci')]
