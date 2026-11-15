CREATE TABLE IF NOT EXISTS connection_policy (
  mcp_path TEXT PRIMARY KEY,
  allowed_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
  mail_folder_id TEXT,
  mail_to JSONB NOT NULL DEFAULT '[]'::jsonb,
  teams_channel_id TEXT,
  teams_chat_id TEXT,
  allow_all BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO connection_policy (mcp_path, allowed_tools, mail_folder_id, mail_to, teams_channel_id, teams_chat_id, allow_all)
VALUES
  ('mail-folder-x', '["list-mail-folder-messages"]'::jsonb, 'folder-x', '[]'::jsonb, NULL, NULL, FALSE),
  ('mail-folder-y', '["list-mail-folder-messages"]'::jsonb, 'folder-y', '[]'::jsonb, NULL, NULL, FALSE),
  ('send_mail', '["send_mail"]'::jsonb, NULL, '["allowed@example.com"]'::jsonb, NULL, NULL, FALSE),
  ('send_teams', '["send-channel-message", "send-chat-message"]'::jsonb, NULL, '[]'::jsonb, 'channel-1', 'chat-1', FALSE)
ON CONFLICT (mcp_path) DO NOTHING;
