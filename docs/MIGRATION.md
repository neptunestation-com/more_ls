# Migrations, pre-flight and drift

`mls migrate OLD NEW` prints the DDL that takes a database deployed from spec OLD to the objects
of spec NEW.  Run `mls preflight NEW --old OLD --dsn ...` first; the migration's VALIDATE phase
aborts on the first row that violates a new constraint, before any policy changes.  After the
migration, `mls drift NEW --dsn ...` must report nothing.  `tests/templates/test_migrate.py`
checks exactly this on Postgres 15–18, then runs the differential test of NEW on the migrated
database.

## Phases (D28)

| # | Phase | Transaction | Contents |
|---|---|---|---|
| 1 | schema | one | new roles (incl. `mls_internal` when first needed), new tables with hardening, new columns, NOT NULL changes, new UNIQUE |
| 2 | constraints | one | new/changed CHECK and FK as `NOT VALID` (direct on new tables; template FKs and depth CHECKs of hierarchies and cardinality limits included), new helper functions with owner and EXECUTE grants, new triggers, replaced functions |
| 3 | indexes | autocommit | `CREATE INDEX CONCURRENTLY` |
| 4 | validate | one per statement | `VALIDATE CONSTRAINT` |
| 5 | swap | one | drop/create policies (incl. `mls_internal_*`), revoke/grant, EXECUTE grant changes, rename changed constraints into place, drop removed constraints and triggers, drop removed helper functions after the policies that referenced them |
| 6 | cleanup | autocommit | `DROP INDEX CONCURRENTLY` |
| 7 | destructive | one, `--allow-destructive` only | `DROP COLUMN`, `DROP TABLE`, then helper functions referenced only by policies of dropped tables |

Between phases 2 and 4 the new constraints are enforced for new writes but not yet for existing
rows; new policies go live only in phase 5, after validation, because the proofs assume stored
rows satisfy the constraints.  Phases 1, 2 and 5 each take ACCESS EXCLUSIVE locks briefly (no
scans); the scans (3, 4) hold SHARE UPDATE EXCLUSIVE, which allows reads and writes.

## Lock modes (D29, observed in `pg_locks` on Postgres 15–18)

| Statement | Lock held on the table |
|---|---|
| `CREATE TABLE`, `CREATE ROLE`, `CREATE [OR REPLACE] FUNCTION`, `ALTER FUNCTION`, `DROP FUNCTION` | none on existing tables |
| `GRANT`, `REVOKE` | none outlives the statement |
| `ALTER TABLE ... ADD COLUMN`, `SET/DROP NOT NULL`, `ADD CONSTRAINT ... UNIQUE` | AccessExclusiveLock |
| `ALTER TABLE ... ADD CONSTRAINT ... CHECK ... NOT VALID` | AccessExclusiveLock (no scan) |
| `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID` | ShareRowExclusiveLock (both tables) |
| `ALTER TABLE ... VALIDATE CONSTRAINT` | ShareUpdateExclusiveLock |
| `ALTER TABLE ... DROP CONSTRAINT`, `RENAME CONSTRAINT` | AccessExclusiveLock |
| `CREATE TRIGGER` | ShareRowExclusiveLock |
| `DROP TRIGGER` | AccessExclusiveLock |
| `CREATE POLICY`, `DROP POLICY` | AccessExclusiveLock |
| `ALTER TABLE ... ENABLE / FORCE ROW LEVEL SECURITY` | AccessExclusiveLock |
| `CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY` | ShareUpdateExclusiveLock |
| `ALTER TABLE ... DROP COLUMN`, `DROP TABLE` | AccessExclusiveLock |

The table above is `LOCKS` in `src/mls/compile/migrate.py`; the generated SQL annotates every
statement with its mode.  If a Postgres version changes a mode, `test_locks.py` fails.

## What is refused

- a column type change (`$.tables.<t>.columns.<c>.type`): add a new column, convert outside MLS,
  then remove the old one;
- a primary-key change (`$.tables.<t>.primary_key`);
- an identity-adapter change (`$.identity.adapter`): every policy would change meaning at once.

A new NOT NULL column has no default; pre-flight reports the row count that would make
`ADD COLUMN` fail.  Dropped roles are never dropped (they are global).

## Pre-flight (D31)

For every new or changed CHECK, FK, NOT NULL and UNIQUE: the count of existing rows that would
be rejected, with samples, computed from the compiled `IS TRUE` predicates in a READ ONLY
transaction.  New or tightened cardinality limits count the rows of over-full groups; a new
hierarchy counts rows beyond the depth bound and rows (members, children) that reference a missing
group; a new limit counts rows that reference a missing parent.  New columns read as NULL (so a check on a new nullable column fails for every
existing row unless it is NULL-safe), new relation tables as empty.

## Drift (D30)

Shadow deploy in a rolled-back transaction, then a normalized catalog comparison.  Reported kinds:
`missing`, `unexpected` (foreign objects on MLS tables), `differs` (expression, validation
state, roles, privileges, nullability, trigger definition, function body / language / SECURITY
DEFINER / owner / EXECUTE grants), `hardening` (D13; also `mls_internal` able to log in or an app
role being a member of it).
