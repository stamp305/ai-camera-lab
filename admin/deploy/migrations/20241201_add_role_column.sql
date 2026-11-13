-- Migration: Add role column to branches table
-- Version: 1.0.1
-- Description: Add role column for the permission matrix (admin/support/viewer)
--
-- OPTIONAL. backend/data_service/core/database/branch_db.py already calls
-- _add_column_if_missing(..., "role", ...) on every start, so a server that
-- boots the backend once does not need this file at all. It is kept for
-- databases that are migrated out-of-band.
--
-- Portable across SQLite and MySQL. Do NOT add Postgres-only statements here
-- (COMMENT ON COLUMN was removed for exactly that reason: sqlite3 aborts the
-- whole script at the first unsupported statement, so everything after it —
-- including the index — silently never ran).

ALTER TABLE branches ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'viewer';

UPDATE branches SET role = 'viewer' WHERE role IS NULL OR role = '';

CREATE INDEX IF NOT EXISTS ix_branches_role ON branches(role);
