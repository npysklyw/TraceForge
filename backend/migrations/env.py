"""Use the same settings as the API; never embed credentials in migration files."""

from alembic import context
from sqlalchemy import create_engine, pool

from benchwarden.persistence.models import Base
from benchwarden.settings import get_settings

url = get_settings().database_url.get_secret_value()

if context.is_offline_mode():
    context.configure(
        url=url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    # Tests supply a connection so each suite can migrate its own isolated schema.
    connection = context.config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(
            url, poolclass=pool.NullPool, connect_args={"options": "-c timezone=UTC"}
        )
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=Base.metadata, compare_type=True
            )
            with context.begin_transaction():
                context.run_migrations()
