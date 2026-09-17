"""Initial V1 schema and rebuildable FTS projection.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE entities (\n\tid VARCHAR NOT NULL, \n\tname VARCHAR NOT NULL, \n\tnormalized_name VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (kind, normalized_name)\n)"
    )
    op.execute(
        "CREATE TABLE messages (\n\tid VARCHAR NOT NULL, \n\tquestion TEXT NOT NULL, \n\tanswer TEXT NOT NULL, \n\tscope VARCHAR NOT NULL, \n\tcitations JSON NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id)\n)"
    )
    op.execute(
        "CREATE TABLE processing_rules (\n\tid VARCHAR NOT NULL, \n\tdimension VARCHAR NOT NULL, \n\tvalue VARCHAR NOT NULL, \n\taction VARCHAR NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id)\n)"
    )
    op.execute(
        "CREATE TABLE sources (\n\tid VARCHAR NOT NULL, \n\tplatform VARCHAR NOT NULL, \n\texternal_id VARCHAR NOT NULL, \n\turl TEXT NOT NULL, \n\ttitle TEXT NOT NULL, \n\tcaption TEXT NOT NULL, \n\tcreator_id VARCHAR NOT NULL, \n\tcreator_name VARCHAR NOT NULL, \n\tcollection VARCHAR NOT NULL, \n\tsemantic_type VARCHAR NOT NULL, \n\ttranscript TEXT NOT NULL, \n\tpolicy_action VARCHAR NOT NULL, \n\tpolicy_reason TEXT NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tcurrent_run_id VARCHAR, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (platform, external_id)\n)"
    )
    op.execute(
        'CREATE TABLE wiki_pages (\n\tid VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\t"key" VARCHAR NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\tcurrent_revision INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (kind, "key")\n)'
    )
    op.execute(
        "CREATE TABLE evidence_units (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\ttext TEXT NOT NULL, \n\tstart_seconds FLOAT, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_id, kind, text), \n\tFOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE jobs (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tpriority INTEGER NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tavailable_at DATETIME NOT NULL, \n\tlocked_at DATETIME, \n\terror TEXT NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE processing_runs (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tlevel INTEGER NOT NULL, \n\tprovider VARCHAR NOT NULL, \n\terror TEXT NOT NULL, \n\tstarted_at DATETIME NOT NULL, \n\tcompleted_at DATETIME, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE source_annotations (\n\tsource_id VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (source_id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE user_states (\n\tid VARCHAR NOT NULL, \n\tentity_id VARCHAR NOT NULL, \n\tstate VARCHAR NOT NULL, \n\trating INTEGER, \n\tnote TEXT NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (entity_id), \n\tFOREIGN KEY(entity_id) REFERENCES entities (id)\n)"
    )
    op.execute(
        "CREATE TABLE vector_documents (\n\tsource_id VARCHAR NOT NULL, \n\tmodel VARCHAR NOT NULL, \n\tvector JSON NOT NULL, \n\tPRIMARY KEY (source_id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE wiki_revisions (\n\tid VARCHAR NOT NULL, \n\tpage_id VARCHAR NOT NULL, \n\tnumber INTEGER NOT NULL, \n\tbody TEXT NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (page_id, number), \n\tFOREIGN KEY(page_id) REFERENCES wiki_pages (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE claims (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\trun_id VARCHAR NOT NULL, \n\tentity_id VARCHAR, \n\tevidence_id VARCHAR NOT NULL, \n\tpredicate VARCHAR NOT NULL, \n\tvalue TEXT NOT NULL, \n\tattribution VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE, \n\tFOREIGN KEY(run_id) REFERENCES processing_runs (id) ON DELETE CASCADE, \n\tFOREIGN KEY(entity_id) REFERENCES entities (id), \n\tFOREIGN KEY(evidence_id) REFERENCES evidence_units (id)\n)"
    )
    op.execute(
        "CREATE TABLE entity_mentions (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\trun_id VARCHAR NOT NULL, \n\tentity_id VARCHAR NOT NULL, \n\tsurface VARCHAR NOT NULL, \n\tevidence_id VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE, \n\tFOREIGN KEY(run_id) REFERENCES processing_runs (id) ON DELETE CASCADE, \n\tFOREIGN KEY(entity_id) REFERENCES entities (id), \n\tFOREIGN KEY(evidence_id) REFERENCES evidence_units (id)\n)"
    )
    op.execute(
        "CREATE TABLE knowledge_items (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\trun_id VARCHAR NOT NULL, \n\tsummary TEXT NOT NULL, \n\tdomain VARCHAR NOT NULL, \n\tform VARCHAR NOT NULL, \n\tcoverage VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE, \n\tUNIQUE (run_id), \n\tFOREIGN KEY(run_id) REFERENCES processing_runs (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE wiki_supports (\n\tid VARCHAR NOT NULL, \n\trevision_id VARCHAR NOT NULL, \n\tclaim_id VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (revision_id, claim_id), \n\tFOREIGN KEY(revision_id) REFERENCES wiki_revisions (id) ON DELETE CASCADE, \n\tFOREIGN KEY(claim_id) REFERENCES claims (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE VIRTUAL TABLE search_fts USING fts5(source_id UNINDEXED, text, tokenize='unicode61')"
    )


def downgrade():
    op.execute("DROP TABLE search_fts")
    op.drop_table("wiki_supports")
    op.drop_table("knowledge_items")
    op.drop_table("entity_mentions")
    op.drop_table("claims")
    op.drop_table("wiki_revisions")
    op.drop_table("vector_documents")
    op.drop_table("user_states")
    op.drop_table("source_annotations")
    op.drop_table("processing_runs")
    op.drop_table("jobs")
    op.drop_table("evidence_units")
    op.drop_table("wiki_pages")
    op.drop_table("sources")
    op.drop_table("processing_rules")
    op.drop_table("messages")
    op.drop_table("entities")
