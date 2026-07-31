import { randomUUID } from "node:crypto";
import { auth, db, trustedAppOrigin } from "@/lib/auth";
import {
  hasExactKeys,
  isRecord,
  isUuid,
  readBoundedJson,
  requestProblemResponse,
  utf8Length,
  validateJsonRequest,
  validateSameOrigin,
} from "@/lib/request-security";

const MAXIMUM_CHAT_HISTORY_REQUEST_BYTES = 320_000;
const MAXIMUM_MESSAGES_PER_CHAT = 80;
const MAXIMUM_MESSAGE_BYTES = 64_000;
const MAXIMUM_TOTAL_MESSAGE_BYTES = 256_000;
const MAXIMUM_METADATA_REQUEST_BYTES = 2_048;
const MAXIMUM_PINNED_CHATS = 20;

type StoredMessage = {
  id: string;
  text: string;
  role: "user" | "assistant";
};

type SaveChatBody = {
  conversationId?: string;
  messages: StoredMessage[];
};

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  headers.set("Cache-Control", "no-store");

  return Response.json(body, {
    ...init,
    headers,
  });
}

function validateMutationHeaders(request: Request): Response | null {
  const originProblem = validateSameOrigin(request, trustedAppOrigin);
  if (originProblem) {
    return requestProblemResponse(originProblem);
  }

  const contentTypeProblem = validateJsonRequest(request);
  if (contentTypeProblem) {
    return requestProblemResponse(contentTypeProblem);
  }

  return null;
}

function parseStoredMessage(value: unknown): StoredMessage | null {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ["id", "role", "text"])
    || typeof value.id !== "string"
    || !isUuid(value.id)
    || typeof value.text !== "string"
    || value.text.trim().length === 0
    || utf8Length(value.text) > MAXIMUM_MESSAGE_BYTES
    || !["user", "assistant"].includes(
      typeof value.role === "string" ? value.role : "",
    )
  ) {
    return null;
  }

  return {
    id: value.id,
    text: value.text,
    role: value.role as StoredMessage["role"],
  };
}

function parseSaveChatBody(value: unknown): SaveChatBody | null {
  if (
    !isRecord(value)
    || !Array.isArray(value.messages)
    || value.messages.length === 0
    || value.messages.length > MAXIMUM_MESSAGES_PER_CHAT
  ) {
    return null;
  }

  const expectedKeys = value.conversationId === undefined
    ? ["messages"]
    : ["conversationId", "messages"];
  if (!hasExactKeys(value, expectedKeys)) {
    return null;
  }

  const conversationId = value.conversationId;
  if (
    conversationId !== undefined
    && (typeof conversationId !== "string" || !isUuid(conversationId))
  ) {
    return null;
  }

  const messages: StoredMessage[] = [];
  let totalMessageBytes = 0;

  for (const candidate of value.messages) {
    const message = parseStoredMessage(candidate);
    if (!message) {
      return null;
    }

    totalMessageBytes += utf8Length(message.text);
    if (totalMessageBytes > MAXIMUM_TOTAL_MESSAGE_BYTES) {
      return null;
    }

    messages.push(message);
  }

  return {
    conversationId,
    messages,
  };
}

async function getUserId(request: Request) {
  const session = await auth.api.getSession({
    headers: request.headers,
  });

  return session?.user.id ?? null;
}

export async function GET(request: Request) {
  const userId = await getUserId(request);

  if (!userId) {
    return jsonResponse({ error: "Unauthorized" }, { status: 401 });
  }

  const conversations = await db.query<{
    id: string;
    title: string;
    is_pinned: boolean;
    updated_at: Date;
  }>(
    `
      SELECT id, title, is_pinned, updated_at
      FROM conversation
      WHERE user_id = $1
        AND (
          id IN (
            SELECT id
            FROM conversation
            WHERE user_id = $1 AND is_pinned = true
            ORDER BY updated_at DESC
            LIMIT 20
          )
          OR id IN (
            SELECT id
            FROM conversation
            WHERE user_id = $1 AND is_pinned = false
            ORDER BY updated_at DESC
            LIMIT 10
          )
        )
      ORDER BY is_pinned DESC, updated_at DESC
    `,
    [userId],
  );

  if (conversations.rows.length === 0) {
    return Response.json(
      { chats: [] },
      {
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  const conversationIds = conversations.rows.map((conversation) => conversation.id);
  const messages = await db.query<{
    id: string;
    conversation_id: string;
    content: string;
    role: "user" | "assistant";
  }>(
    `
      SELECT id, conversation_id, content, role
      FROM message
      WHERE conversation_id = ANY($1::text[])
      ORDER BY conversation_id, position
    `,
    [conversationIds],
  );

  const messagesByConversation = new Map<string, StoredMessage[]>();

  for (const message of messages.rows) {
    const currentMessages = messagesByConversation.get(message.conversation_id) ?? [];
    currentMessages.push({
      id: message.id,
      text: message.content,
      role: message.role,
    });
    messagesByConversation.set(message.conversation_id, currentMessages);
  }

  return Response.json(
    {
      chats: conversations.rows.map((conversation) => ({
        id: conversation.id,
        title: conversation.title,
        isPinned: conversation.is_pinned,
        updatedAt: conversation.updated_at.toISOString(),
        messages: messagesByConversation.get(conversation.id) ?? [],
      })),
    },
    {
      headers: {
        "Cache-Control": "no-store",
      },
    },
  );
}

export async function POST(request: Request) {
  const userId = await getUserId(request);

  if (!userId) {
    return jsonResponse({ error: "Unauthorized" }, { status: 401 });
  }

  const headerProblem = validateMutationHeaders(request);
  if (headerProblem) {
    return headerProblem;
  }

  const parsedBody = await readBoundedJson(
    request,
    MAXIMUM_CHAT_HISTORY_REQUEST_BYTES,
  );
  if (!parsedBody.ok) {
    return requestProblemResponse(parsedBody.problem);
  }

  const body = parseSaveChatBody(parsedBody.value);
  if (!body) {
    return jsonResponse({ error: "Invalid chat messages" }, { status: 400 });
  }

  const messages = body.messages;

  const conversationId = body.conversationId ?? randomUUID();
  const firstUserMessage = messages.find((message) => message.role === "user");
  const title = firstUserMessage?.text.trim().slice(0, 60) || "New chat";
  const client = await db.connect();
  let isPinned = false;

  try {
    await client.query("BEGIN");

    if (body.conversationId) {
      const updatedConversation = await client.query<{ is_pinned: boolean }>(
        `
          UPDATE conversation
          SET title = $1, updated_at = now()
          WHERE id = $2 AND user_id = $3
          RETURNING is_pinned
        `,
        [title, conversationId, userId],
      );

      if (updatedConversation.rowCount === 0) {
        await client.query("ROLLBACK");
        return jsonResponse({ error: "Chat not found" }, { status: 404 });
      }

      isPinned = updatedConversation.rows[0].is_pinned;
    } else {
      await client.query(
        `
          INSERT INTO conversation (id, user_id, title)
          VALUES ($1, $2, $3)
        `,
        [conversationId, userId, title],
      );
    }

    await client.query(
      "DELETE FROM message WHERE conversation_id = $1",
      [conversationId],
    );

    for (const [position, message] of messages.entries()) {
      await client.query(
        `
          INSERT INTO message (id, conversation_id, role, content, position)
          VALUES ($1, $2, $3, $4, $5)
        `,
        [message.id, conversationId, message.role, message.text, position],
      );
    }

    await client.query(
      `
        DELETE FROM conversation
        WHERE user_id = $1
          AND is_pinned = false
          AND id NOT IN (
            SELECT id
            FROM conversation
            WHERE user_id = $1 AND is_pinned = false
            ORDER BY updated_at DESC
            LIMIT 10
          )
      `,
      [userId],
    );

    await client.query("COMMIT");

    return jsonResponse({
      chat: {
        id: conversationId,
        title,
        isPinned,
        updatedAt: new Date().toISOString(),
        messages,
      },
    });
  } catch (error) {
    await client.query("ROLLBACK");
    console.error("Failed to save chat:", error);
    return jsonResponse({ error: "Unable to save chat" }, { status: 500 });
  } finally {
    client.release();
  }
}

export async function PATCH(request: Request) {
  const userId = await getUserId(request);

  if (!userId) {
    return jsonResponse({ error: "Unauthorized" }, { status: 401 });
  }

  const headerProblem = validateMutationHeaders(request);
  if (headerProblem) {
    return headerProblem;
  }

  const parsedBody = await readBoundedJson(
    request,
    MAXIMUM_METADATA_REQUEST_BYTES,
  );
  if (!parsedBody.ok) {
    return requestProblemResponse(parsedBody.problem);
  }

  if (
    !isRecord(parsedBody.value)
    || !hasExactKeys(parsedBody.value, ["conversationId", "isPinned"])
    || typeof parsedBody.value.conversationId !== "string"
    || !isUuid(parsedBody.value.conversationId)
    || typeof parsedBody.value.isPinned !== "boolean"
  ) {
    return jsonResponse({ error: "Invalid pin request" }, { status: 400 });
  }

  const conversationId = parsedBody.value.conversationId;
  const isPinned = parsedBody.value.isPinned;

  const client = await db.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      "SELECT pg_advisory_xact_lock(hashtext($1))",
      [userId],
    );
    const currentConversation = await client.query<{ is_pinned: boolean }>(
      `
        SELECT is_pinned
        FROM conversation
        WHERE id = $1 AND user_id = $2
        FOR UPDATE
      `,
      [conversationId, userId],
    );

    if (currentConversation.rowCount === 0) {
      await client.query("ROLLBACK");
      return jsonResponse({ error: "Chat not found" }, { status: 404 });
    }

    if (isPinned && !currentConversation.rows[0].is_pinned) {
      const pinnedCount = await client.query<{ count: string }>(
        `
          SELECT COUNT(*)::text AS count
          FROM conversation
          WHERE user_id = $1 AND is_pinned = true
        `,
        [userId],
      );
      if (Number(pinnedCount.rows[0].count) >= MAXIMUM_PINNED_CHATS) {
        await client.query("ROLLBACK");
        return jsonResponse(
          { error: "Pinned chat limit reached" },
          { status: 429 },
        );
      }
    }

    const updatedConversation = await client.query<{
      id: string;
      is_pinned: boolean;
    }>(
      `
        UPDATE conversation
        SET is_pinned = $1
        WHERE id = $2 AND user_id = $3
        RETURNING id, is_pinned
      `,
      [isPinned, conversationId, userId],
    );
    await client.query("COMMIT");

    return jsonResponse({
      chat: {
        id: updatedConversation.rows[0].id,
        isPinned: updatedConversation.rows[0].is_pinned,
      },
    });
  } catch (error) {
    await client.query("ROLLBACK");
    console.error("Failed to update chat pin:", error);
    return jsonResponse(
      { error: "Unable to update chat pin" },
      { status: 500 },
    );
  } finally {
    client.release();
  }
}

export async function DELETE(request: Request) {
  const userId = await getUserId(request);

  if (!userId) {
    return jsonResponse({ error: "Unauthorized" }, { status: 401 });
  }

  const headerProblem = validateMutationHeaders(request);
  if (headerProblem) {
    return headerProblem;
  }

  const parsedBody = await readBoundedJson(
    request,
    MAXIMUM_METADATA_REQUEST_BYTES,
  );
  if (!parsedBody.ok) {
    return requestProblemResponse(parsedBody.problem);
  }

  if (
    !isRecord(parsedBody.value)
    || !hasExactKeys(parsedBody.value, ["conversationId"])
    || typeof parsedBody.value.conversationId !== "string"
    || !isUuid(parsedBody.value.conversationId)
  ) {
    return jsonResponse({ error: "Invalid conversation ID" }, { status: 400 });
  }

  const conversationId = parsedBody.value.conversationId;

  try {
    const deletedConversation = await db.query<{ id: string }>(
      `
        DELETE FROM conversation
        WHERE id = $1 AND user_id = $2
        RETURNING id
      `,
      [conversationId, userId],
    );

    if (deletedConversation.rowCount === 0) {
      return jsonResponse({ error: "Chat not found" }, { status: 404 });
    }

    return jsonResponse({
      deletedChatId: deletedConversation.rows[0].id,
    });
  } catch (error) {
    console.error("Failed to delete chat:", error);
    return jsonResponse({ error: "Unable to delete chat" }, { status: 500 });
  }
}
