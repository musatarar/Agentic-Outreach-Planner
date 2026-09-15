"""Drop the LLM catalog: provider/model/key selection moves to the environment.

``LLM_PROVIDER`` picks the adapter, ``LLM_MODEL`` optionally overrides its
default model, and the key stays in the provider's own env var -- so the
singleton configuration row, the seeded catalog it pointed at and the encrypted
key column all stop being state anyone has to manage.

Hand-ordered rather than left as `makemigrations` emitted it. The autodetector
prefaces the deletes with `AlterUniqueTogether` + `RemoveField` on
`llmmodel.provider`, which SQLite serves by rebuilding the table before
dropping it. Dropping whole tables in FK order needs no rebuild and takes the
unique_together and the `single_llm_configuration` check constraint with them.
Same end state; `makemigrations --check` stays clean.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0013_one_review_flow"),
    ]

    operations = [
        # Referents first: llmconfiguration -> llmprovider, llmmodel;
        # llmmodel -> llmprovider.
        migrations.DeleteModel(name="LLMConfiguration"),
        migrations.DeleteModel(name="LLMModel"),
        migrations.DeleteModel(name="LLMProvider"),
    ]
