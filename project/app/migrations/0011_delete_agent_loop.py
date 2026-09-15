"""Drop the agent loop's tables: the flag-gated copy loop and its call audit.

Hand-ordered rather than left as `makemigrations` emitted it. The autodetector
prefaces the deletes with `RemoveField` on `agentstep.lead_run`, which SQLite
serves by rebuilding the table -- and the rebuild re-reads a model that no
longer has the field its `astep_one_seq_per_run` constraint names, so it dies.
Dropping whole tables in FK order needs no rebuild and takes the constraints
with them. Same end state; `makemigrations --check` stays clean.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0010_delete_dead_send_machinery"),
    ]

    operations = [
        # Referents first: agentstep -> agentleadrun, providertrace;
        # providertracecontent -> providertrace.
        migrations.DeleteModel(name="AEAvailabilitySlot"),
        migrations.DeleteModel(name="AgentStep"),
        migrations.DeleteModel(name="AgentLeadRun"),
        migrations.DeleteModel(name="ProviderTraceContent"),
        migrations.DeleteModel(name="ProviderTrace"),
    ]
