-- Add tools_json column to store discovered MCP tools
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS tools_json JSONB;
