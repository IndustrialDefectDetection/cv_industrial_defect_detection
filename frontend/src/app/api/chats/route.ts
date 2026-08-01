import { randomUUID } from "node:crypto";
import { auth, db, trustedAppOrigin } from "@/lib/auth";
// Chat history mutations append one authenticated user message at a time.
import {
  MAXIMUM_STORED_MESSAGE_BYTES,
  canAppendStoredMessage,
  parseAppendUserMessageBody,
  type StoredMessage,
} from "@/lib/chat-history";
import {
  hasExactKeys,
  isRecord,
  isUuid,
  readBoundedJson,
  requestProblemResponse,
  validateJsonRequest,
  validateSameOrigin,
} from "@/lib/request-security";

const MAXIMUM_CHAT_HISTORY_REQUEST_BYTES = 320_000;
const MAXIMUM_METADATA_REQUEST_BYTES = 2_048;
const MAXIMUM_PINNED_CHATS = 20;

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

  const body = parseAppendUserMessageBody(parsedBody.value);
  if (!body) {
    return jsonResponse(
      { error: "Request must append exactly one user message" },
      { status: 400 },
    );
  }

  const client = await db.connect();

  try {
    await client.query("BEGIN");
    await client.query(
      "SELECT pg_advisory_xact_lock(hashtext($1))",
      [userId],
    );

    const existingMessage = await client.query<{
      conversation_id: string;
      role: "user" | "assistant";
      content: string;
      user_id: string;
    }>(
      `
        SELECT m.conversation_id, m.role, m.content, c.user_id
        FROM message AS m
        INNER JOIN conversation AS c ON c.id = m.conversation_id
        WHERE m.id = $1
      `,
      [body.message.id],
    );

    let conversationId = body.conversationId;
    if (!conversationId && existingMessage.rowCount) {
      const existing = existingMessage.rows[0];
      if (
        existing.user_id !== userId
        || existing.role !== "user"
        || existing.content !== body.message.text
      ) {
        await client.query("ROLLBACK");
        return jsonResponse(
          { error: "Message conflicts with saved chat history" },
          { status: 409 },
        );
      }
      conversationId = existing.conversation_id;
    }

    const title = body.message.text.trim().slice(0, 60) || "New chat";
    let conversation: {
      id: string;
      title: string;
      is_pinned: boolean;
      updated_at: Date;
    };

    if (conversationId) {
      const currentConversation = await client.query<{
        id: string;
        title: string;
        is_pinned: boolean;
        updated_at: Date;
      }>(
        `
          SELECT id, title, is_pinned, updated_at
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

      conversation = currentConversation.rows[0];
    } else {
      conversationId = randomUUID();
      const createdConversation = await client.query<{
        id: string;
        title: string;
        is_pinned: boolean;
        updated_at: Date;
      }>(
        `
          INSERT INTO conversation (id, user_id, title)
          VALUES ($1, $2, $3)
          RETURNING id, title, is_pinned, updated_at
        `,
        [conversationId, userId, title],
      );
      conversation = createdConversation.rows[0];
    }

    const existing = existingMessage.rows[0];
    if (
      existing
      && (
        existing.user_id !== userId
        || existing.conversation_id !== conversationId
        || existing.role !== "user"
        || existing.content !== body.message.text
      )
    ) {
      await client.query("ROLLBACK");
      return jsonResponse(
        { error: "Message conflicts with saved chat history" },
        { status: 409 },
      );
    }

    if (!existing) {
      const totals = await client.query<{
        message_count: number;
        total_bytes: number;
      }>(
        `
          SELECT
            COUNT(*)::integer AS message_count,
            COALESCE(SUM(octet_length(content)), 0)::integer AS total_bytes
          FROM message
          WHERE conversation_id = $1
        `,
        [conversationId],
      );

      if (!canAppendStoredMessage(
        totals.rows[0].message_count,
        totals.rows[0].total_bytes,
        body.message.text,
        1,
        MAXIMUM_STORED_MESSAGE_BYTES,
      )) {
        await client.query("ROLLBACK");
        return jsonResponse(
          { error: "Chat history limit reached; start a new chat" },
          { status: 409 },
        );
      }

      const nextPosition = await client.query<{ position: number }>(
        `
          SELECT COALESCE(MAX(position), -1)::integer + 1 AS position
          FROM message
          WHERE conversation_id = $1
        `,
        [conversationId],
      );
      const insertedMessage = await client.query<{ id: string }>(
        `
          INSERT INTO message (id, conversation_id, role, content, position)
          VALUES ($1, $2, 'user', $3, $4)
          ON CONFLICT (id) DO NOTHING
          RETURNING id
        `,
        [
          body.message.id,
          conversationId,
          body.message.text,
          nextPosition.rows[0].position,
        ],
      );

      if (insertedMessage.rowCount === 0) {
        const conflict = await client.query<{
          conversation_id: string;
          role: "user" | "assistant";
          content: string;
          user_id: string;
        }>(
          `
            SELECT m.conversation_id, m.role, m.content, c.user_id
            FROM message AS m
            INNER JOIN conversation AS c ON c.id = m.conversation_id
            WHERE m.id = $1
          `,
          [body.message.id],
        );
        const matching = conflict.rows[0];
        if (
          !matching
          || matching.user_id !== userId
          || matching.conversation_id !== conversationId
          || matching.role !== "user"
          || matching.content !== body.message.text
        ) {
          await client.query("ROLLBACK");
          return jsonResponse(
            { error: "Message conflicts with saved chat history" },
            { status: 409 },
          );
        }
      }
    }

    const updatedConversation = await client.query<{
      updated_at: Date;
    }>(
      `
        UPDATE conversation
        SET updated_at = now()
        WHERE id = $1 AND user_id = $2
        RETURNING updated_at
      `,
      [conversationId, userId],
    );
    conversation = {
      ...conversation,
      updated_at: updatedConversation.rows[0].updated_at,
    };

    await client.query(
      `
        DELETE FROM conversation AS stale
        WHERE stale.user_id = $1
          AND stale.is_pinned = false
          AND stale.id NOT IN (
            SELECT id
            FROM conversation
            WHERE user_id = $1 AND is_pinned = false
            ORDER BY updated_at DESC
            LIMIT 10
          )
          AND (
            COALESCE(
              (
                SELECT role
                FROM message
                WHERE conversation_id = stale.id
                ORDER BY position DESC
                LIMIT 1
              ),
              'assistant'
            ) <> 'user'
            OR stale.updated_at < now() - interval '15 minutes'
          )
      `,
      [userId],
    );

    const savedMessages = await client.query<{
      id: string;
      role: "user" | "assistant";
      content: string;
    }>(
      `
        SELECT id, role, content
        FROM message
        WHERE conversation_id = $1
        ORDER BY position
      `,
      [conversationId],
    );

    await client.query("COMMIT");

    return jsonResponse({
      chat: {
        id: conversation.id,
        title: conversation.title,
        isPinned: conversation.is_pinned,
        updatedAt: conversation.updated_at.toISOString(),
        messages: savedMessages.rows.map((message) => ({
          id: message.id,
          role: message.role,
          text: message.content,
        })),
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
