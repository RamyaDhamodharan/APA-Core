# Database integration tests

The tests in `tests/integration` use a real PostgreSQL database and run the Core
Alembic migrations before the test session. They only accept a database named
exactly `ap_test`. If `TEST_DATABASE_URL` is not set, pytest skips these tests with
an explanatory message.

## Run with Docker Compose

Start the Compose stack, then create the disposable database from the Postgres
container. The command uses the configured container database user and does not
display its password:

```powershell
docker compose up -d postgres core
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE ap_test;"'
```

Apply the Auth schema to this disposable database so Core migrations and user
factories can reference `auth.users`:

```powershell
docker compose exec -e DATABASE_URL='postgresql+psycopg://<user>:<password>@postgres:5432/ap_test' auth alembic upgrade head
```

Set `TEST_DATABASE_URL` to the `ap_test` database using the same connection details
already configured for the Compose Postgres service. Run the integration suite in
the Core container:

```powershell
docker compose exec -e TEST_DATABASE_URL='postgresql+psycopg://<user>:<password>@postgres:5432/ap_test' core uv run --no-sync pytest -q tests/integration
```

Replace the placeholders locally; do not paste a real URL into logs or source
control. The fixture verifies the database name before running `alembic upgrade
head`. It does not create or delete databases. The transactional fixture rolls back
per-test writes; the concurrent-duplicate test removes its committed test rows.

The provided factories cover vendors, purchase orders and lines, goods receipts and
lines, invoices and lines, admins, and associates. Tests that exercise mail use an
empty allowlist and assert that drafts remain unsent.
