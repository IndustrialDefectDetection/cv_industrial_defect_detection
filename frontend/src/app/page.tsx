"use client";
import Link from "next/link";
import { Fragment, useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { authClient } from "@/lib/auth-client";
import AppSidebar from "@/components/app-sidebar";
import {
  buildChatTurnPayload,
  getCancelAcknowledgementState,
  getChatComposerControlState,
  getChatSidebarDisabledState,
  mergeSavedChatPreservingPin,
  runPersistedChatTurn,
  shouldApplyAnalysisToActiveChat,
} from "@/lib/chat-interactions";
import { MAXIMUM_USER_INPUT_CHARACTERS } from "@/lib/chat-history";

type Message = {
  id: string;
  text: string;
  role: "user" | "assistant";
}

type Chat = {
  id: string;
  title: string;
  isPinned: boolean;
  updatedAt: string;
  messages: Message[];
};

type ChatResponse = {
  analysis?: string;
  messageId?: string;
  status?: "cancelled";
};

type ChatStreamEvent =
  | { type: "started" | "heartbeat" }
  | { type: "result"; data: ChatResponse }
  | { type: "error"; error: string };

type TraceProgressEvent = {
  seq: number;
  agent: string | null;
  kind: string;
  tool: string | null;
};

type TraceProgressResponse = {
  seq: number;
  run: {
    id: string | null;
    status: string;
  };
  current: {
    agent: string | null;
    tool: string | null;
  };
  events: TraceProgressEvent[];
};

// The Streamlit trace dashboard (TRACE_API.md) runs beside this app and shows
// the run this chat only summarises: every agent, tool call and SQL query.
// Overridable because the port is only fixed on a local machine.
const traceDashboardUrl =
  process.env.NEXT_PUBLIC_TRACE_DASHBOARD_URL ?? "http://localhost:8502";

const agentProgressLabels: Record<string, string> = {
  Chat: "Understanding your request…",
  Supervisor: "Coordinating the analysis…",
  Monitor: "Reviewing production signals…",
  Analyzer: "Analyzing defect data…",
  Planner: "Building an action plan…",
  Verifier: "Verifying the findings…",
  Executor: "Preparing the final response…",
};

function getTraceProgressLabel(agent: string | null, tool: string | null) {
  if (tool?.includes("generate_pdf")) {
    return "Generating the report…";
  }
  if (tool?.includes("create_action_plan")) {
    return "Building an action plan…";
  }
  if (tool?.includes("validate")) {
    return "Verifying the findings…";
  }

  return agent
    ? agentProgressLabels[agent] ?? "Working through the analysis…"
    : "Working through the analysis…";
}

function orderChats(chatList: Chat[]) {
  return [...chatList].sort((firstChat, secondChat) => {
    if (firstChat.isPinned !== secondChat.isPinned) {
      return firstChat.isPinned ? -1 : 1;
    }

    return new Date(secondChat.updatedAt).getTime()
      - new Date(firstChat.updatedAt).getTime();
  });
}

function retainRecentChats(chatList: Chat[]) {
  const orderedChats = orderChats(chatList);
  const pinnedChats = orderedChats.filter((chat) => chat.isPinned);
  const recentChats = orderedChats
    .filter((chat) => !chat.isPinned)
    .slice(0, 10);

  return [...pinnedChats, ...recentChats];
}

type ThemeMode = "light" | "dark" | "system";
export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [chats, setChats] = useState<Chat[]>([]);
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [enteringChatId, setEnteringChatId] = useState<string | null>(null);
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const [deletingChatId, setDeletingChatId] = useState<string | null>(null);
  const [pinningChatId, setPinningChatId] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const hasMessages = messages.length > 0;
  const [isWaiting, setIsWaiting] = useState(false);
  const [isStartingTurn, setIsStartingTurn] = useState(false);
  const [runningChatId, setRunningChatId] = useState<string | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [traceProgress, setTraceProgress] = useState("Starting the analysis…");
  const [cancelledMessageIds, setCancelledMessageIds] = useState<string[]>([]);
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const [themeMode, setThemeMode] = useState<ThemeMode>("system")
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const activeChatIdRef = useRef<string | null>(null);
  const cancelRequestedRef = useRef(false);
  const cancelAcceptedRef = useRef(false);
  const chatStartedRef = useRef(false);
  const { data: session, isPending: isSessionPending } = authClient.useSession();
  const shouldGateChat = !session;

  //System theme handling
  useEffect(() => {
    const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
    const applyTheme = () => {
      const useDarkMode = themeMode === "dark" || (themeMode === "system" && systemTheme.matches);
      document.documentElement.classList.toggle(
        "dark",
        useDarkMode,
      );
    }
    applyTheme();
    systemTheme.addEventListener("change", applyTheme)
    return () => {
      systemTheme.removeEventListener("change", applyTheme)

    };
  },
    [themeMode])
  useEffect(() => {
    activeChatIdRef.current = activeChatId;
  }, [activeChatId]);

  const disabledSidebarActions = getChatSidebarDisabledState({
    isHistoryLoading,
    isStartingTurn,
    isAnalysisRunning: isWaiting,
    deletingChatId,
    pinningChatId,
  });
  const isViewingRunningChat = isWaiting
    && activeChatId === runningChatId;
  const composerControl = getChatComposerControlState({
    isHistoryLoading,
    isAnalysisRunning: isWaiting,
    isViewingRunningChat,
    isCancelling,
    deletingChatId,
    pinningChatId,
  });

  // Scroll to the end when the visible conversation changes.
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [messages]);

  // Progress updates belong only to the conversation that started the run.
  useEffect(() => {
    if (!isViewingRunningChat) {
      return;
    }

    messagesEndRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [isViewingRunningChat, traceProgress]);

  useEffect(() => {
    if (!session || !isWaiting || isCancelling) {
      return;
    }

    const controller = new AbortController();
    let cursor = 0;
    let runId: string | null = null;
    let activeAgent: string | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function pollTrace() {
      try {
        const response = await fetch(`/api/chat/trace?since=${cursor}`, {
          cache: "no-store",
          signal: controller.signal,
        });

        if (!response.ok) {
          return;
        }

        const trace = await response.json() as TraceProgressResponse;

        if (trace.seq < cursor) {
          cursor = 0;
          runId = null;
          activeAgent = null;
          setTraceProgress("Starting the analysis…");
          return;
        }

        cursor = trace.seq;
        if (trace.run.status !== "running") {
          return;
        }

        if (trace.run.id !== runId) {
          runId = trace.run.id;
          activeAgent = null;
        }

        for (const event of trace.events) {
          if (
            (event.kind === "agent_start" || event.kind === "tool_start")
            && event.agent
          ) {
            activeAgent = event.agent;
          } else if (
            event.kind === "agent_end"
            && event.agent === activeAgent
          ) {
            activeAgent = null;
          }
        }

        const currentAgent = trace.current.agent ?? activeAgent;
        setTraceProgress(getTraceProgressLabel(
          currentAgent,
          trace.current.tool,
        ));
      } catch (error) {
        if (error instanceof Error && error.name === "AbortError") {
          return;
        }
      } finally {
        if (!controller.signal.aborted) {
          timer = setTimeout(pollTrace, 900);
        }
      }
    }

    pollTrace();

    return () => {
      controller.abort();
      if (timer) {
        clearTimeout(timer);
      }
    };
  }, [isCancelling, isWaiting, session]);

  useEffect(() => {
    if (!session) {
      return;
    }

    const controller = new AbortController();

    async function loadChats() {
      setIsHistoryLoading(true);
      setHistoryError(null);

      try {
        const response = await fetch("/api/chats", {
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error("Unable to load chat history");
        }

        const data = await response.json() as { chats: Chat[] };
        setChats(orderChats(data.chats));
      } catch (error) {
        if (error instanceof Error && error.name !== "AbortError") {
          setHistoryError("Could not load chat history.");
        }
      } finally {
        if (!controller.signal.aborted) {
          setIsHistoryLoading(false);
        }
      }
    }

    loadChats();

    return () => controller.abort();
  }, [session]);

  function markMessageCancelled(messageId: string) {
    setCancelledMessageIds((currentIds) =>
      currentIds.includes(messageId)
        ? currentIds
        : [...currentIds, messageId]
    );
  }

  function updateChatMessages(
    chatId: string | null,
    nextMessages: Message[],
  ) {
    if (chatId === null) {
      return;
    }

    setChats((currentChats) =>
      currentChats.map((chat) =>
        chat.id === chatId
          ? { ...chat, messages: nextMessages }
          : chat
      )
    );
  }

  async function handleSend() {
    if (
      !isHistoryLoading
      && !isWaiting
      && !deletingChatId
      && !pinningChatId
      && input.trim() !== ""
    ) {
      const newMessage: Message & { role: "user" } = {
        id: crypto.randomUUID(),
        text: input,
        role: "user"
      };
      const sourceChatId = activeChatIdRef.current;
      const pendingMessages = [...messages, newMessage];
      let persistedMessages = pendingMessages;
      setMessages(pendingMessages);
      updateChatMessages(sourceChatId, pendingMessages);
      setInput("");
      setHistoryError(null);
      cancelRequestedRef.current = false;
      cancelAcceptedRef.current = false;
      chatStartedRef.current = false;
      setTraceProgress("Starting the analysis…");
      setRunningChatId(sourceChatId);
      setIsStartingTurn(true);
      setIsWaiting(true);
      try {
        const turn = await runPersistedChatTurn({
          savePending: async () => {
            const savedChat = await savePendingMessage(
              newMessage,
              sourceChatId,
            );
            if (savedChat) {
              persistedMessages = savedChat.messages;
            }
            return savedChat;
          },
          onPersisted: (persistedChatId) => {
            setIsStartingTurn(false);
            setRunningChatId(persistedChatId);
            updateChatMessages(persistedChatId, persistedMessages);
          },
          isCancelled: () => cancelRequestedRef.current,
          requestAnalysis: (persistedChatId) => getResponse(
            newMessage.text,
            persistedChatId,
            newMessage.id,
          ),
        });
        if (turn.status === "save_failed") {
          setMessages(messages);
          updateChatMessages(sourceChatId, messages);
          setInput(newMessage.text);
          return;
        }

        if (turn.status === "cancelled") {
          markMessageCancelled(newMessage.id);
          return;
        }

        const persistedChatId = turn.conversationId;
        const response = turn.response;

        if (response.status === "cancelled") {
          markMessageCancelled(newMessage.id);
          return;
        }

        if (
          typeof response.analysis !== "string"
          || typeof response.messageId !== "string"
        ) {
          throw new Error("Assistant response did not include analysis");
        }

        const chatResponse: Message = {
          id: response.messageId,
          text: response.analysis,
          role: "assistant"
        }
        const completedMessages = [...persistedMessages, chatResponse];
        updateChatMessages(persistedChatId, completedMessages);
        if (shouldApplyAnalysisToActiveChat(
          activeChatIdRef.current,
          persistedChatId,
        )) {
          setMessages(completedMessages);
        }
      } catch {
        if (cancelRequestedRef.current) {
          markMessageCancelled(newMessage.id);
        } else {
          setHistoryError("The assistant could not respond.");
        }
      } finally {
        cancelRequestedRef.current = false;
        cancelAcceptedRef.current = false;
        chatStartedRef.current = false;
        setIsCancelling(false);
        setIsStartingTurn(false);
        setRunningChatId(null);
        setIsWaiting(false);
      }
    }
  }

  async function cancelPrompt() {
    if (!isWaiting || isCancelling) {
      return;
    }

    cancelRequestedRef.current = true;
    setIsCancelling(true);
    setHistoryError(null);

    try {
      const response = await fetch("/api/chat/cancel", {
        method: "POST",
      });

      if (!response.ok) {
        throw new Error("Unable to cancel prompt");
      }
      const data = await response.json() as { cancelling?: boolean };
      const acknowledgement = getCancelAcknowledgementState(
        data.cancelling === true,
        chatStartedRef.current,
      );
      cancelAcceptedRef.current = acknowledgement === "accepted";
      if (acknowledgement === "rejected") {
        cancelRequestedRef.current = false;
        setIsCancelling(false);
        setHistoryError("The current response could not be cancelled.");
      }
    } catch {
      if (cancelRequestedRef.current) {
        cancelRequestedRef.current = false;
        setIsCancelling(false);
        setHistoryError("The current response could not be cancelled.");
      }
    }
  }

  async function savePendingMessage(
    message: Message & { role: "user" },
    conversationId: string | null,
  ): Promise<Chat | null> {
    setHistoryError(null);

    try {
      const response = await fetch("/api/chats", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          conversationId: conversationId ?? undefined,
          message,
        }),
      });

      if (!response.ok) {
        throw new Error("Unable to save chat");
      }

      const data = await response.json() as { chat: Chat };
      if (conversationId === null) {
        setEnteringChatId(data.chat.id);
      }
      if (shouldApplyAnalysisToActiveChat(
        activeChatIdRef.current,
        conversationId,
      )) {
        if (conversationId === null) {
          activeChatIdRef.current = data.chat.id;
          setActiveChatId(data.chat.id);
        }
        setMessages(data.chat.messages);
      }
      setChats((currentChats) => retainRecentChats(
        mergeSavedChatPreservingPin(currentChats, data.chat),
      ));
      return data.chat;
    } catch {
      setHistoryError("This chat could not be saved.");
      return null;
    }
  }

  function startNewChat() {
    if (disabledSidebarActions.newChat) {
      return;
    }

    activeChatIdRef.current = null;
    setActiveChatId(null);
    setEnteringChatId(null);
    setCancelledMessageIds([]);
    setMessages([]);
    setInput("");
    setHistoryError(null);
  }

  function selectChat(chatId: string) {
    if (
      chatId === activeChatId
      || disabledSidebarActions.selectChat
    ) {
      return;
    }

    const selectedChat = chats.find((chat) => chat.id === chatId);

    if (selectedChat) {
      activeChatIdRef.current = selectedChat.id;
      setActiveChatId(selectedChat.id);
      setCancelledMessageIds([]);
      setMessages(selectedChat.messages);
      setInput("");
      setHistoryError(null);
    }
  }

  async function setChatPinned(chatId: string, isPinned: boolean) {
    if (disabledSidebarActions.pinChat) {
      return;
    }

    setPinningChatId(chatId);
    setHistoryError(null);

    try {
      const response = await fetch("/api/chats", {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ conversationId: chatId, isPinned }),
      });

      if (!response.ok) {
        throw new Error("Unable to update chat pin");
      }

      const data = await response.json() as {
        chat: Pick<Chat, "id" | "isPinned">;
      };
      setChats((currentChats) => orderChats(
        currentChats.map((chat) =>
          chat.id === data.chat.id
            ? { ...chat, isPinned: data.chat.isPinned }
            : chat
        )
      ));
    } catch {
      setHistoryError("Could not update this chat's pin.");
    } finally {
      setPinningChatId(null);
    }
  }

  async function deleteChat(chatId: string) {
    if (disabledSidebarActions.deleteChat) {
      return;
    }

    setDeletingChatId(chatId);
    setHistoryError(null);

    try {
      const response = await fetch("/api/chats", {
        method: "DELETE",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ conversationId: chatId }),
      });

      if (!response.ok) {
        throw new Error("Unable to delete chat");
      }

      setChats((currentChats) =>
        currentChats.filter((chat) => chat.id !== chatId)
      );

      if (activeChatIdRef.current === chatId) {
        activeChatIdRef.current = null;
        setActiveChatId(null);
        setCancelledMessageIds([]);
        setMessages([]);
        setInput("");
      }
    } catch {
      setHistoryError("Could not delete chat.");
    } finally {
      setDeletingChatId(null);
    }
  }

  async function handleLogout() {
    setIsLoggingOut(true);
    setHistoryError(null);

    try {
      const result = await authClient.signOut();

      if (result.error) {
        setHistoryError(result.error.message ?? "Unable to log out.");
        return;
      }

      window.location.assign("/sign-in");
    } finally {
      setIsLoggingOut(false);
    }
  }

  async function getResponse(
    userInput: string,
    conversationId: string,
    messageId: string,
  ): Promise<ChatResponse> {
    const response = await fetch(
      "/api/chat",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(buildChatTurnPayload(
          conversationId,
          messageId,
          userInput,
        )),
      }
    )

    if (!response.ok) {
      throw new Error("Assistant request failed");
    }

    if (!response.body) {
      throw new Error("Assistant response stream was unavailable");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });

      let newlineIndex = buffer.indexOf("\n");
      while (newlineIndex !== -1) {
        const line = buffer.slice(0, newlineIndex).trim();
        buffer = buffer.slice(newlineIndex + 1);
        newlineIndex = buffer.indexOf("\n");

        if (!line) {
          continue;
        }

        const event = JSON.parse(line) as ChatStreamEvent;
        if (event.type === "started") {
          chatStartedRef.current = true;
        }
        if (
          event.type === "started"
          && cancelRequestedRef.current
          && !cancelAcceptedRef.current
        ) {
          // A click can race the brief gap between saving the pending user
          // message and the backend acquiring its run slot. Once "started"
          // arrives, retry cancellation against the run we now know exists.
          let cancellationAccepted = false;
          try {
            const cancellationResponse = await fetch("/api/chat/cancel", {
              method: "POST",
            });
            if (cancellationResponse.ok) {
              const cancellation = await cancellationResponse.json() as {
                cancelling?: boolean;
              };
              cancellationAccepted = cancellation.cancelling === true;
              cancelAcceptedRef.current = cancellationAccepted;
            }
          } catch {
            // Keep reading the original stream. Its terminal event remains
            // the source of truth for when the run guard has been released.
          }
          if (!cancellationAccepted && cancelRequestedRef.current) {
            cancelRequestedRef.current = false;
            setIsCancelling(false);
            setHistoryError("The current response could not be cancelled.");
          }
        }
        if (event.type === "result") {
          return event.data;
        }
        if (event.type === "error") {
          throw new Error(event.error);
        }
      }

      if (done) {
        break;
      }
    }

    throw new Error("Assistant response ended before completion");
  }

  return (
    <>
      <main
        className={`relative flex min-h-screen bg-[radial-gradient(circle_at_15%_12%,_#3b82f61f_0%,_transparent_30%),radial-gradient(circle_at_88%_18%,_#8b5cf619_0%,_transparent_32%),radial-gradient(circle_at_65%_88%,_#14b8a614_0%,_transparent_36%),linear-gradient(135deg,_#f8fafc_0%,_#e8eef8_50%,_#eef2f7_100%)] text-slate-900 transition-[filter,opacity] duration-300 dark:bg-[linear-gradient(145deg,_#0d0e10_0%,_#090a0c_55%,_#0c0d0f_100%)] dark:text-zinc-100 ${shouldGateChat ? "pointer-events-none select-none blur-[3px]" : ""
          }`}
      >

      <div
        aria-hidden="true"
        className={`absolute inset-x-0 top-0 z-10 h-16 border-b border-slate-200/60 bg-[linear-gradient(90deg,rgba(255,255,255,0.72),rgba(248,250,252,0.52),rgba(255,255,255,0.64))] backdrop-blur-2xl transition-[opacity,transform,box-shadow] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] dark:border-[#24262b] dark:bg-[linear-gradient(90deg,rgba(12,13,15,0.97),rgba(7,8,10,0.94),rgba(12,13,15,0.96))] ${hasMessages
            ? "translate-y-0 opacity-100 shadow-[0_10px_32px_rgba(71,85,105,0.09)]"
            : "-translate-y-3 opacity-0 shadow-none"
          }`}
      />

      <div
        aria-hidden={hasMessages}
        className={`absolute top-[calc(50%-6rem)] z-20 whitespace-nowrap text-4xl font-semibold tracking-tight text-slate-800 transition-[opacity,transform] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none dark:text-slate-100 ${session
          ? "left-[calc(50%+var(--app-sidebar-half-width))]"
          : "left-1/2"
          } ${hasMessages
            ? "pointer-events-none -translate-x-1/2 -translate-y-2 scale-[0.98] opacity-0"
            : "-translate-x-1/2 translate-y-0 scale-100 opacity-100 delay-100"
          }`}
      >
        <span>
          Defect
          <span className="ml-1.5 text-blue-600 [text-shadow:0_0_6px_rgba(59,130,246,0.28),0_0_12px_rgba(96,165,250,0.16)] dark:text-blue-400 dark:[text-shadow:none]">
            Detection
          </span>
        </span>
      </div>

      {/* Sits below the composer on the landing screen only. The chat can
          answer questions about the factory, but it cannot show the agents
          working - that is the trace dashboard, and nothing here previously
          told anyone it existed. */}
      <div
        aria-hidden={hasMessages}
        className={`absolute top-[calc(50%+4rem)] z-20 text-center text-sm text-slate-500 transition-[opacity,transform] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none dark:text-zinc-500 ${session
          ? "left-[calc(50%+var(--app-sidebar-half-width))]"
          : "left-1/2"
          } ${hasMessages
            ? "pointer-events-none -translate-x-1/2 -translate-y-2 opacity-0"
            : "-translate-x-1/2 translate-y-0 opacity-100 delay-200"
          }`}
      >
        Ask about defects, machines, work orders or downtime — or open{" "}
        <a
          href={traceDashboardUrl}
          target="_blank"
          rel="noreferrer noopener"
          className="font-medium text-blue-600 underline-offset-4 hover:underline dark:text-blue-400"
        >
          developer mode
        </a>{" "}
        to watch the agents work.
      </div>

      <header
        aria-hidden={!hasMessages}
        className={`absolute left-8 top-0 z-20 flex h-16 items-center whitespace-nowrap text-xl font-semibold tracking-tight text-slate-800 transition-[opacity,transform] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none dark:text-slate-100 ${hasMessages
          ? "translate-y-0 opacity-100 delay-150"
          : "pointer-events-none -translate-y-2 opacity-0"
          }`}
      >
        <span>
          Defect
          <span className="ml-1.5 text-blue-600 [text-shadow:0_0_6px_rgba(59,130,246,0.28),0_0_12px_rgba(96,165,250,0.16)] dark:text-blue-400 dark:[text-shadow:none]">
            Detection
          </span>
        </span>
      </header>

      <div className="absolute right-4 top-0 z-20 flex h-16 items-center gap-2">
        <fieldset className="theme-switch w-48">
          <legend className="sr-only">Color theme</legend>

          <input
            id="theme-light"
            name="theme"
            type="radio"
            checked={themeMode === "light"}
            onChange={() => setThemeMode("light")}
          />
          <label htmlFor="theme-light">
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              className="h-3.5 w-3.5"
            >
              <circle cx="12" cy="12" r="3.5" />
              <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
            </svg>
            <span>Light</span>
          </label>

          <input
            id="theme-dark"
            name="theme"
            type="radio"
            checked={themeMode === "dark"}
            onChange={() => setThemeMode("dark")}
          />
          <label htmlFor="theme-dark">
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-3.5 w-3.5"
            >
              <path d="M20.5 15.2A8.5 8.5 0 0 1 8.8 3.5 8.5 8.5 0 1 0 20.5 15.2Z" />
            </svg>
            <span>Dark</span>
          </label>

          <input
            id="theme-system"
            name="theme"
            type="radio"
            checked={themeMode === "system"}
            onChange={() => setThemeMode("system")}
          />
          <label htmlFor="theme-system">
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-3.5 w-3.5"
            >
              <rect x="3" y="4" width="18" height="13" rx="2" />
              <path d="M8 21h8M12 17v4" />
            </svg>
            <span>Auto</span>
          </label>
        </fieldset>
        <a
          href={traceDashboardUrl}
          target="_blank"
          rel="noreferrer noopener"
          title="Watch every agent, tool call and SQL query behind an answer"
          className="rounded-full border border-slate-300/80 px-3 py-1.5 text-xs font-medium text-slate-600 transition-colors hover:border-slate-400 hover:text-slate-900 dark:border-[#2f3238] dark:text-zinc-400 dark:hover:border-zinc-600 dark:hover:text-zinc-100"
        >
          Developer mode
        </a>
        {session && (
          <button
            type="button"
            onClick={handleLogout}
            disabled={isLoggingOut}
            className="logout-button"
          >
            {isLoggingOut ? "Logging out…" : "Log out"}
          </button>
        )}
      </div>

      {session && (
        <AppSidebar
          hasMessages={hasMessages}
          chats={chats}
          activeChatId={activeChatId}
          enteringChatId={enteringChatId}
          isLoading={isHistoryLoading}
          deletingChatId={deletingChatId}
          pinningChatId={pinningChatId}
          disabledActions={disabledSidebarActions}
          error={historyError}
          onNewChat={startNewChat}
          onSetChatPinned={setChatPinned}
          onSelectChat={selectChat}
          onDeleteChat={deleteChat}
          onChatEnterEnd={(chatId) => {
            setEnteringChatId((currentChatId) =>
              currentChatId === chatId ? null : currentChatId
            );
          }}
        />
      )}
      <section className="flex flex-1 relative flex-col justify-center items-center">
        {/*User and assistant messages*/}
        <div
          className={`absolute left-1/2 flex max-h-[calc(100vh-12rem)] w-[min(92%,64rem)] -translate-x-1/2 flex-col overflow-y-auto pl-1 pr-4 [scrollbar-gutter:stable] transition-[top] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] ${hasMessages
            ? "message-scroll-fade top-20 pb-1 pt-8"
            : "top-1/2 py-1"
            }`}
        >
          {messages.map((message, index) => (
            <Fragment key={message.id}>
              {message.role === "user" ? (
                <div className="message-enter-user mb-4 ml-auto flex w-fit max-w-[70%] justify-end rounded-lg bg-black/95 p-2 pl-3 pr-3 text-white shadow-lg dark:bg-zinc-800 dark:text-zinc-100 dark:shadow-[0_8px_20px_rgba(0,0,0,0.28)]">
                  {message.text}
                </div>
              ) : (
                <div className="message-enter-assistant mb-9 mr-auto min-w-0 w-full px-2 pb-2">
                  <div className="mb-3 flex items-center gap-2.5 text-sm font-normal text-slate-600 dark:text-zinc-400">
                    <span
                      className={`assistant-logo ${index === messages.length - 1 ? "assistant-logo-latest" : ""}`}
                      aria-hidden="true"
                    />
                    <span>Defect Assistant</span>
                  </div>
                  <div className="assistant-response [overflow-wrap:anywhere] text-left text-base text-slate-800 dark:text-zinc-200">
                    <Markdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        img: ({ alt }) => (
                          <span
                            role="note"
                            className="text-sm italic text-slate-500 dark:text-zinc-400"
                          >
                            {alt
                              ? `[Image omitted: ${alt}]`
                              : "[Image omitted]"}
                          </span>
                        ),
                        table: ({ children }) => (
                          <div className="assistant-table-wrap">
                            <table>{children}</table>
                          </div>
                        ),
                      }}
                    >
                      {message.text}
                    </Markdown>
                  </div>
                </div>
              )}

              {message.role === "user"
                && cancelledMessageIds.includes(message.id)
                && (
                  <div
                    role="status"
                    aria-label="Assistant analysis cancelled"
                    className="mb-6 flex w-full items-center gap-2 px-2 text-xs font-medium text-slate-400 dark:text-zinc-500"
                  >
                    <span
                      aria-hidden="true"
                      className="h-px flex-1 bg-slate-300/65 dark:bg-zinc-700/70"
                    />
                    <span>Cancelled analysis</span>
                    <span
                      aria-hidden="true"
                      className="h-px flex-1 bg-slate-300/65 dark:bg-zinc-700/70"
                    />
                  </div>
                )}
            </Fragment>
          ))}
          {isViewingRunningChat && (
            <div
              role="status"
              aria-label={isCancelling
                ? "Cancelling assistant response"
                : traceProgress}
              className="mb-6 mr-auto px-2 py-1"
            >
              <div
                aria-hidden="true"
                className={`generating-loader ${isCancelling
                  ? "generating-loader--cancelling"
                  : ""
                  }`}
              >
                <div className="generating-loader__spinner" />
                {isCancelling ? (
                  <span className="generating-loader__cancel-label">
                    Cancelling…
                  </span>
                ) : (
                  <span
                    key={traceProgress}
                    className="generating-loader__progress-label"
                  >
                    {traceProgress}
                  </span>
                )}
              </div>
            </div>
          )}
          <div ref={messagesEndRef} aria-hidden="true" />
        </div>
        <footer
          className={`absolute left-1/2 z-[6] flex w-[min(92%,64rem)] -translate-x-1/2 rounded-2xl border border-white/80 bg-white/70 p-2 text-slate-800 shadow-lg backdrop-blur-xl transition-[top,box-shadow] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] dark:border-[#292b31] dark:bg-[#111216]/98 dark:text-zinc-100 dark:shadow-[0_18px_46px_rgba(0,0,0,0.38)] ${hasMessages
              ? "top-[calc(100%-5.5rem)] shadow-[0_14px_36px_rgba(71,85,105,0.16)]"
              : "top-1/2"
            }`}
        >
          <div className="flex w-full items-center gap-3">
            <input
              className="flex-1 bg-transparent px-4
             placeholder:text-slate-600 outline-0 dark:placeholder:text-zinc-500"
              type="text"
              maxLength={MAXIMUM_USER_INPUT_CHARACTERS}
              placeholder="Type here...."
              disabled={deletingChatId !== null || pinningChatId !== null}
              value={input ?? ""}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key == "Enter") {
                  handleSend()
                }
              }}
            />
            <button
              type="button"
              aria-label={composerControl.action === "cancel"
                ? isCancelling
                  ? "Cancelling response"
                  : "Stop generating"
                : "Send message"}
              disabled={composerControl.disabled}
              className={`rounded-full p-2 transition-colors disabled:cursor-not-allowed disabled:bg-slate-400 disabled:text-white/70 dark:disabled:bg-zinc-700 dark:disabled:text-zinc-400 ${composerControl.action === "cancel"
                ? "bg-slate-700 text-white hover:bg-red-600 dark:bg-zinc-200 dark:text-zinc-950 dark:hover:bg-red-500 dark:hover:text-white"
                : "bg-black/95 text-white/90 dark:bg-zinc-100 dark:text-zinc-950"
                }`}
              onClick={composerControl.action === "cancel"
                ? cancelPrompt
                : handleSend}
            >
              {composerControl.action === "cancel" ? (
                <svg
                  aria-hidden="true"
                  viewBox="0 0 24 24"
                  className="h-5 w-5"
                >
                  <rect
                    x="7"
                    y="7"
                    width="10"
                    height="10"
                    rx="1.5"
                    fill="currentColor"
                  />
                </svg>
              ) : (
                <svg
                  aria-hidden="true"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-5 w-5"
                >
                  <path d="M12 19V5" />
                  <path d="m5 12 7-7 7 7" />
                </svg>
              )}
            </button>
          </div>
        </footer>

      </section>
      </main>

      {shouldGateChat && (
        <div className="session-gate-overlay fixed inset-0 z-50 flex items-center justify-center px-6">
          {isSessionPending ? (
            <div
              role="status"
              aria-live="polite"
              className="session-check-status"
            >
              <span className="session-loader-frame" aria-hidden="true">
                <span className="session-grid-loader" />
              </span>
              <span className="session-check-label">Checking session</span>
            </div>
          ) : (
            <section className="w-full max-w-sm rounded-2xl border border-white/75 bg-white/82 p-7 text-center shadow-[0_24px_70px_rgba(30,41,59,0.24)] backdrop-blur-2xl dark:border-[#303238] dark:bg-[#111216]/94 dark:shadow-[0_28px_80px_rgba(0,0,0,0.58)]">
              <h2 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-zinc-100">
                Welcome
              </h2>
              <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-zinc-400">
                Sign in or create an account to start a manufacturing analysis.
              </p>
              <div className="mt-6 grid gap-3">
                <Link
                  href="/sign-in"
                  className="rounded-xl bg-slate-900 px-5 py-3 font-medium text-white shadow-lg transition hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:bg-zinc-100 dark:text-zinc-950 dark:hover:bg-white"
                >
                  Sign in
                </Link>
                <Link
                  href="/sign-in?mode=signUp"
                  className="rounded-xl border border-slate-300/80 bg-white/55 px-5 py-3 font-medium text-slate-700 transition hover:border-slate-400 hover:bg-white/85 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-zinc-700 dark:bg-zinc-900/70 dark:text-zinc-200 dark:hover:border-zinc-500 dark:hover:bg-zinc-800"
                >
                  Create account
                </Link>
              </div>
            </section>
          )}
        </div>
      )}
    </>
  );
}
