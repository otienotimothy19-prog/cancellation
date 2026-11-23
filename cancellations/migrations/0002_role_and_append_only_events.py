"""Create the "cancellations" role group and make case_event append-only at the database level."""

from django.db import migrations

PG_FORWARD = """
CREATE OR REPLACE FUNCTION cancellations_caseevent_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'cancellations_caseevent is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS cancellations_caseevent_no_update_delete ON cancellations_caseevent;
CREATE TRIGGER cancellations_caseevent_no_update_delete
    BEFORE UPDATE OR DELETE ON cancellations_caseevent
    FOR EACH ROW EXECUTE FUNCTION cancellations_caseevent_append_only();
"""

PG_REVERSE = """
DROP TRIGGER IF EXISTS cancellations_caseevent_no_update_delete ON cancellations_caseevent;
DROP FUNCTION IF EXISTS cancellations_caseevent_append_only();
"""

SQLITE_FORWARD = [
    """CREATE TRIGGER IF NOT EXISTS cancellations_caseevent_no_update
       BEFORE UPDATE ON cancellations_caseevent
       BEGIN SELECT RAISE(ABORT, 'cancellations_caseevent is append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS cancellations_caseevent_no_delete
       BEFORE DELETE ON cancellations_caseevent
       BEGIN SELECT RAISE(ABORT, 'cancellations_caseevent is append-only'); END;""",
]
SQLITE_REVERSE = [
    "DROP TRIGGER IF EXISTS cancellations_caseevent_no_update;",
    "DROP TRIGGER IF EXISTS cancellations_caseevent_no_delete;",
]


def add_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        # Raw cursor, no params: the PL/pgSQL body contains a literal "%" that the schema
        # editor would otherwise treat as a query placeholder.
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(PG_FORWARD)
    elif vendor == "sqlite":
        for sql in SQLITE_FORWARD:
            schema_editor.execute(sql)


def drop_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(PG_REVERSE)
    elif vendor == "sqlite":
        for sql in SQLITE_REVERSE:
            schema_editor.execute(sql)


def create_role(apps, schema_editor):
    from django.contrib.auth.management import create_permissions

    app_config = apps.get_app_config("cancellations")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name="cancellations")
    perm = Permission.objects.filter(codename="manage_cancellations", content_type__app_label="cancellations").first()
    if perm:
        group.permissions.add(perm)


def remove_role(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name="cancellations").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("cancellations", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(create_role, remove_role),
        migrations.RunPython(add_triggers, drop_triggers),
    ]
