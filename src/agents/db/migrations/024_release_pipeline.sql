-- Migration 024: Release pipeline support
-- Adds columns to deliverables for tracking build artifacts and commit SHA

ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS release_artifact_json JSONB;
ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS head_sha TEXT;
