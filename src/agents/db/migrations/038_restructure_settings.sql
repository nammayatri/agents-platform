-- Restructure settings_json into namespaced format.
-- This migration runs a one-time transformation on all projects.
-- The migration is idempotent — it checks for the new format before transforming.
-- The Python _migrate_settings() function handles the full migration at read time;
-- this SQL does a best-effort bulk update so most rows are pre-migrated.

UPDATE projects
SET settings_json = jsonb_build_object(
    'planning', jsonb_build_object(
        'require_approval', COALESCE((settings_json->>'require_plan_approval')::boolean, false),
        'guidelines', ''
    ),
    'execution', jsonb_build_object(
        'work_rules', COALESCE(settings_json->'work_rules', '{}'::jsonb),
        'architect_editor', jsonb_build_object(
            'enabled', COALESCE(architect_editor_enabled, false),
            'architect_model', architect_model,
            'editor_model', editor_model
        ),
        'max_iterations', 500
    ),
    'git', jsonb_build_object(
        'merge_method', COALESCE(settings_json->>'merge_method', 'squash'),
        'require_merge_approval', COALESCE((settings_json->>'require_merge_approval')::boolean, false),
        'build_commands', COALESCE(settings_json->'build_commands', '[]'::jsonb),
        'post_merge_actions', COALESCE(settings_json->'post_merge_actions', '{}'::jsonb)
    ),
    'debugging', COALESCE(settings_json->'debug_context', '{}'::jsonb),
    'release', jsonb_build_object(
        'enabled', COALESCE((settings_json->>'release_pipeline_enabled')::boolean, false),
        'webhooks', '[]'::jsonb
    ),
    'understanding', jsonb_build_object(
        'status', settings_json->>'analysis_status',
        'project', COALESCE(settings_json->'project_understanding', '{}'::jsonb),
        'dependencies', COALESCE(settings_json->'dep_understandings', '{}'::jsonb),
        'linking', COALESCE(settings_json->'linking_document', '{}'::jsonb)
    )
) || (
    -- Preserve keys that don't map into any section
    COALESCE(
        jsonb_strip_nulls(jsonb_build_object(
            'index_metadata', settings_json->'index_metadata',
            'merge_pipelines', settings_json->'merge_pipelines',
            'release_configs', settings_json->'release_configs',
            'release_config', settings_json->'release_config'
        )),
        '{}'::jsonb
    )
)
WHERE settings_json IS NOT NULL
  AND settings_json != '{}'::jsonb
  AND NOT (settings_json ? 'planning');  -- skip if already migrated
