-- Migration 035: Add compaction_summary to chat sessions.
-- Stores the LLM-generated conversation summary from Tier 2/3 compaction
-- so it can be re-injected on session reload without re-running the LLM.

ALTER TABLE project_chat_sessions
    ADD COLUMN IF NOT EXISTS compaction_summary TEXT;
