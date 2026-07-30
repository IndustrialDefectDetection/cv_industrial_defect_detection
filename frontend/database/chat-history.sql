CREATE TABLE IF NOT EXISTS conversation (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  title text NOT NULL,
  is_pinned boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE conversation
  ADD COLUMN IF NOT EXISTS is_pinned boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS conversation_user_updated_idx
  ON conversation (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS conversation_user_pinned_updated_idx
  ON conversation (user_id, is_pinned DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS message (
  id text PRIMARY KEY,
  conversation_id text NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  content text NOT NULL,
  position integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, position)
);

CREATE INDEX IF NOT EXISTS message_conversation_position_idx
  ON message (conversation_id, position);
