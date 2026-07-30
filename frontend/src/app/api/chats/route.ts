import { randomUUID } from "node:crypto";
import { auth, db } from "@/lib/auth";

type StoredMessage = {
  id: string;
  text: string;
  role: "user" | "assistant";
};

type SaveChatBody = {
  conversationId?: string;
  messages?: StoredMessage[];
};

async function getUserId(request: Request) {
  const session = await auth.api.getSession({
    headers: request.headers,
  });

  return session?.user.id ?? null;
}

export async function GET(request: Request) {
  const userId = await getUserId(request);

  if (!userId) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
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
          is_pinned = true
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
    return Response.json({ chats: [] });
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

  return Response.json({
    chats: conversations.rows.map((conversation) => ({
      id: conversation.id,
      title: conversation.title,
      isPinned: conversation.is_pinned,
      updatedAt: conversation.updated_at.toISOString(),
      messages: messagesByConversation.get(conversation.id) ?? [],
    })),
  });
}

export async function POST(request: Request) {
  const userId = await getUserId(request);

  if (!userId) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const body = await request.json() as SaveChatBody;
  const messages = body.messages;

  if (
    !Array.isArray(messages)
    || messages.length === 0
    || messages.some((message) =>
      typeof message?.id !== "string"
      || typeof message?.text !== "string"
      || !["user", "assistant"].includes(message?.role)
    )
  ) {
    return Response.json({ error: "Invalid chat messages" }, { status: 400 });
  }

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
        return Response.json({ error: "Chat not found" }, { status: 404 });
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

    return Response.json({
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
    return Response.json({ error: "Unable to save chat" }, { status: 500 });
  } finally {
    client.release();
  }
}

export async function PATCH(request: Request) {
  const userId = await getUserId(request);

  if (!userId) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  let body: unknown;

  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "Invalid request body" }, { status: 400 });
  }

  const conversationIdValue = typeof body === "object" && body !== null
    && "conversationId" in body
    ? body.conversationId
    : undefined;
  const isPinnedValue = typeof body === "object" && body !== null
    && "isPinned" in body
    ? body.isPinned
    : undefined;

  if (
    typeof conversationIdValue !== "string"
    || conversationIdValue.trim() === ""
    || conversationIdValue.length > 128
    || typeof isPinnedValue !== "boolean"
  ) {
    return Response.json({ error: "Invalid pin request" }, { status: 400 });
  }

  const conversationId = conversationIdValue.trim();

  try {
    const updatedConversation = await db.query<{
      id: string;
      is_pinned: boolean;
    }>(
      `
        UPDATE conversation
        SET is_pinned = $1
        WHERE id = $2 AND user_id = $3
        RETURNING id, is_pinned
      `,
      [isPinnedValue, conversationId, userId],
    );

    if (updatedConversation.rowCount === 0) {
      return Response.json({ error: "Chat not found" }, { status: 404 });
    }

    return Response.json({
      chat: {
        id: updatedConversation.rows[0].id,
        isPinned: updatedConversation.rows[0].is_pinned,
      },
    });
  } catch (error) {
    console.error("Failed to update chat pin:", error);
    return Response.json({ error: "Unable to update chat pin" }, { status: 500 });
  }
}

export async function DELETE(request: Request) {
  const userId = await getUserId(request);

  if (!userId) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  let body: unknown;

  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "Invalid request body" }, { status: 400 });
  }

  const conversationIdValue = typeof body === "object" && body !== null
    && "conversationId" in body
    ? body.conversationId
    : undefined;

  if (
    typeof conversationIdValue !== "string"
    || conversationIdValue.trim() === ""
    || conversationIdValue.length > 128
  ) {
    return Response.json({ error: "Invalid conversation ID" }, { status: 400 });
  }

  const conversationId = conversationIdValue.trim();

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
      return Response.json({ error: "Chat not found" }, { status: 404 });
    }

    return Response.json({
      deletedChatId: deletedConversation.rows[0].id,
    });
  } catch (error) {
    console.error("Failed to delete chat:", error);
    return Response.json({ error: "Unable to delete chat" }, { status: 500 });
  }
}
