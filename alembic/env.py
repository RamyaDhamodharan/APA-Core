import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool, text

from alembic import context
from src.models import (
    audit_models,
    document_models,
    document_workflow_models,
    email_models,
    exchange_rate_models,
    goods_receipt_models,
    invoice_match_models,
    invoice_models,
    invoice_supporting_document_models,
    match_run_models,
    notification_models,
    purchase_order_models,
    user_models,
)
from src.models.vendor_models import Base

# Alembic discovers mapped tables from the model modules imported above.
_MODEL_MODULES = (
    audit_models,
    document_models,
    document_workflow_models,
    email_models,
    exchange_rate_models,
    goods_receipt_models,
    invoice_models,
    invoice_match_models,
    invoice_supporting_document_models,
    match_run_models,
    notification_models,
    purchase_order_models,
    user_models,
)

load_dotenv()
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise RuntimeError("DATABASE_URL environment variable is not set")

config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and getattr(object, "schema", None) != "core":
        return False
    return not (reflected and compare_to is None and type_ in {"table", "column"})


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in (None, "core", "auth")
    if type_ == "table":
        return parent_names.get("schema_name") in (None, "core")
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
        include_name=include_name,
        include_schemas=True,
        version_table="core_alembic_version",
        version_table_schema="core",
    )

    # Core revisions use unqualified table and enum names; mirror the online
    # connection's lookup order in generated SQL as well.
    context.execute("SET search_path TO core, auth, public")

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS core"))
        connection.commit()
        connection.execute(text("SET search_path TO core, auth, public"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
            include_name=include_name,
            include_schemas=True,
            version_table="core_alembic_version",
            version_table_schema="core",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
