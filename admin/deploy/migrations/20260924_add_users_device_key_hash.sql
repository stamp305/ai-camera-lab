-- Migration: Add device_key_hash column to users table
-- Description: SHA-256 hex digest of the Branch's Device Key (ADR 0007).
-- NULL means no key on record; clearing it back to NULL reopens Activation.
--
-- OPTIONAL. backend/app_ai_camera_backend.py calls
-- add_device_key_hash_column_if_missing() (data_service/core/database/crud/auth_crud.py)
-- on every start, so a server that boots the backend once does not need this
-- file at all. It is kept for databases that are migrated out-of-band.
--
-- Plain ALTER TABLE only, no vendor-specific clauses (IF NOT EXISTS, AFTER,
-- COMMENT). If the column already exists the statement fails with a
-- duplicate-column error and changes nothing.

ALTER TABLE users ADD COLUMN device_key_hash CHAR(64) NULL;
