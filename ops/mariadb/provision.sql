-- HireEdge — MariaDB/MySQL provisioning (run as an ADMIN / root account).
--
-- Purpose:
--   1. Create a dedicated, least-privilege application user scoped to ONE database.
--   2. Grant access to the test database so `manage.py test` (MYSQL_USE_FOR_TESTS=1)
--      and `manage.py dbrestore --verify` can create/use it.
--   3. Optionally enforce TLS for the app user.
--
-- The running app user does NOT need global privileges. Scoping to `<APP_DB>`.* and
-- `test\_<APP_DB>`.* is the least privilege that still supports migrations, tests,
-- and restore verification.
--
-- Replace the placeholders before running:
--   <APP_DB>       e.g. resxjob
--   <APP_USER>     e.g. resxapp
--   <APP_PASSWORD> a strong, unique password (NOT the admin password)

-- --- 1. Application database (idempotent) ---------------------------------
CREATE DATABASE IF NOT EXISTS `<APP_DB>`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- --- 2. Dedicated app user (LAN + localhost) ------------------------------
-- Use '%' for LAN access from app/huey hosts, or lock to a specific subnet/host.
CREATE USER IF NOT EXISTS '<APP_USER>'@'%'
  IDENTIFIED BY '<APP_PASSWORD>';

-- Full privileges on the application schema only (migrations need DDL).
GRANT ALL PRIVILEGES ON `<APP_DB>`.* TO '<APP_USER>'@'%';

-- Test database: Django prefixes the DB name with `test_`. Granting on the
-- pattern lets the user CREATE/DROP it during the test run.
GRANT ALL PRIVILEGES ON `test\_<APP_DB>`.* TO '<APP_USER>'@'%';

-- Optional: a scratch DB for `dbrestore --target restore_scratch` verification.
-- CREATE DATABASE IF NOT EXISTS `restore_scratch`
--   CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- GRANT ALL PRIVILEGES ON `restore_scratch`.* TO '<APP_USER>'@'%';

-- --- 3. Optional: require TLS for this user (server already supports it) ---
-- ALTER USER '<APP_USER>'@'%' REQUIRE SSL;

FLUSH PRIVILEGES;

-- --- Verify -----------------------------------------------------------------
-- SHOW GRANTS FOR '<APP_USER>'@'%';
