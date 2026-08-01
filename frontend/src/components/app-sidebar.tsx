import type { ChatSidebarDisabledState } from "@/lib/chat-interactions";

type AppSidebarProps = {
  hasMessages?: boolean;
  chats: {
    id: string;
    title: string;
    isPinned: boolean;
  }[];
  activeChatId: string | null;
  enteringChatId: string | null;
  isLoading: boolean;
  deletingChatId: string | null;
  pinningChatId: string | null;
  disabledActions: ChatSidebarDisabledState;
  error: string | null;
  onNewChat: () => void;
  onSetChatPinned: (chatId: string, isPinned: boolean) => void;
  onSelectChat: (chatId: string) => void;
  onDeleteChat: (chatId: string) => void;
  onChatEnterEnd: (chatId: string) => void;
};

export default function AppSidebar({
  hasMessages = false,
  chats,
  activeChatId,
  enteringChatId,
  isLoading,
  deletingChatId,
  pinningChatId,
  disabledActions,
  error,
  onNewChat,
  onSetChatPinned,
  onSelectChat,
  onDeleteChat,
  onChatEnterEnd,
}: AppSidebarProps) {
  return (
    <aside
      className={`app-sidebar-reveal relative z-[5] flex w-[var(--app-sidebar-width)] shrink-0 flex-col overflow-hidden border-r border-slate-200/60 p-4 pt-20 backdrop-blur-2xl transition-[background-color,box-shadow] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)] dark:border-[#1b1c20] dark:bg-[linear-gradient(180deg,_#101114_0%,_#090a0c_100%)] ${hasMessages
        ? "bg-white/38 shadow-[10px_0_36px_rgba(71,85,105,0.09)] dark:shadow-[8px_0_24px_rgba(0,0,0,0.18)]"
        : "bg-white/28 shadow-[8px_0_30px_rgba(71,85,105,0.07)] dark:shadow-[6px_0_20px_rgba(0,0,0,0.14)]"
        }`}
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-4 top-16 h-px bg-gradient-to-r from-transparent via-slate-300/70 to-transparent dark:via-zinc-800/50"
      />
      <div
        aria-hidden="true"
        className={`pointer-events-none absolute -left-20 top-24 h-56 w-56 rounded-full bg-blue-400/10 blur-3xl transition-opacity duration-700 dark:bg-transparent ${hasMessages ? "opacity-100" : "opacity-60"
          }`}
      />
      <button
        type="button"
        onClick={onNewChat}
        disabled={disabledActions.newChat}
        className="relative w-fit rounded-md border border-slate-300/80 bg-white/65 px-3 py-2 text-left text-sm font-normal text-slate-700 shadow-sm transition hover:border-blue-400 hover:bg-white disabled:cursor-not-allowed disabled:opacity-55 dark:border-[#303238] dark:bg-[#17181c] dark:text-zinc-200 dark:hover:border-zinc-500 dark:hover:bg-[#202126]"
      >
        + New chat
      </button>

      <div className="relative mt-6 min-h-0 min-w-0 flex-1">
        <h2 className="px-1 text-sm font-normal text-slate-500 dark:text-zinc-500">
          Recent chats
        </h2>

        {isLoading ? (
          <p className="px-1 pt-4 text-sm text-slate-500 dark:text-zinc-500">
            Loading…
          </p>
        ) : chats.length === 0 ? (
          <p className="px-1 pt-4 text-sm leading-6 text-slate-500 dark:text-zinc-500">
            No previous chats
          </p>
        ) : (
          <div className="mt-3 grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1.5">
            {chats.map((chat) => {
              const isActive = activeChatId === chat.id;
              const isDeleting = deletingChatId === chat.id;
              const isPinning = pinningChatId === chat.id;
              const isEntering = enteringChatId === chat.id;

              return (
                <div
                  key={chat.id}
                  className={isEntering ? "sidebar-chat-enter" : "min-w-0 w-full"}
                  onAnimationEnd={(event) => {
                    if (isEntering && event.currentTarget === event.target) {
                      onChatEnterEnd(chat.id);
                    }
                  }}
                >
                  <div className={`${isEntering ? "sidebar-chat-enter-content " : ""}group relative min-w-0 w-full transition-opacity ${isPinning ? "opacity-60" : ""}`}>
                    <button
                      type="button"
                      onClick={() => onSelectChat(chat.id)}
                      disabled={disabledActions.selectChat}
                      className={`block min-w-0 w-full truncate rounded-md border py-2.5 pl-4 pr-[5rem] text-left text-sm font-normal shadow-[0_3px_10px_rgba(71,85,105,0.04)] transition disabled:cursor-not-allowed disabled:opacity-55 dark:shadow-[0_4px_12px_rgba(0,0,0,0.12)] ${isActive
                        ? "border-blue-200/80 bg-blue-100/90 text-blue-950 dark:border-[#383b44] dark:bg-[#25272d] dark:text-zinc-100"
                        : "border-slate-200/70 bg-white/45 text-slate-700 hover:border-slate-300/80 hover:bg-white/75 hover:text-slate-950 dark:border-[#23252a] dark:bg-[#131418]/85 dark:text-zinc-300 dark:hover:border-[#303238] dark:hover:bg-[#191a1f] dark:hover:text-zinc-100"
                        }`}
                      title={chat.title}
                    >
                      {chat.title}
                    </button>

                    <div className="absolute right-1.5 top-1/2 flex -translate-y-1/2 items-center gap-1">
                      <button
                        type="button"
                        onClick={() => onSetChatPinned(chat.id, !chat.isPinned)}
                        disabled={disabledActions.pinChat}
                        aria-label={isPinning
                          ? `Updating pin for ${chat.title}`
                          : chat.isPinned
                            ? `Unpin ${chat.title}`
                            : `Pin ${chat.title}`}
                        aria-pressed={chat.isPinned}
                        aria-busy={isPinning}
                        title={chat.isPinned ? "Unpin chat" : "Pin chat"}
                        className={`grid h-7 w-7 place-items-center rounded-md border transition-[border-color,background-color,color,transform] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-blue-500 active:scale-95 disabled:cursor-not-allowed disabled:opacity-35 ${chat.isPinned
                          ? "border-blue-200/80 bg-blue-50/90 text-blue-600 hover:bg-blue-100 dark:border-blue-400/20 dark:bg-blue-500/10 dark:text-blue-300 dark:hover:bg-blue-500/15"
                          : "border-transparent text-slate-400 hover:border-slate-200/80 hover:bg-blue-50 hover:text-blue-600 dark:text-zinc-500 dark:hover:border-[#303238] dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
                          }`}
                      >
                        <svg
                          aria-hidden="true"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.7"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          className="h-4 w-4"
                        >
                          <path d="M12 17v5" />
                          <path d="M9 10.75a2 2 0 0 0-1.1.9l-1.8 2.57a1 1 0 0 0 .82 1.58h10.16a1 1 0 0 0 .82-1.58l-1.8-2.57a2 2 0 0 0-1.1-.9V5a3 3 0 0 0 1.5-2.6.4.4 0 0 0-.4-.4H7.1a.4.4 0 0 0-.4.4A3 3 0 0 0 8.2 5v5.75Z" />
                        </svg>
                      </button>

                      <button
                        type="button"
                        onClick={() => onDeleteChat(chat.id)}
                        disabled={disabledActions.deleteChat}
                        aria-label={isDeleting
                          ? `Deleting ${chat.title}`
                          : `Delete ${chat.title}`}
                        aria-busy={isDeleting}
                        title={isDeleting ? "Deleting chat" : "Delete chat"}
                        className="grid h-7 w-7 place-items-center rounded-md border border-transparent text-base leading-none text-slate-400 opacity-70 transition-[border-color,background-color,color,opacity,transform] hover:border-red-200/70 hover:bg-red-50 hover:text-red-600 focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-red-500 active:scale-95 disabled:cursor-not-allowed disabled:opacity-35 dark:text-zinc-500 dark:hover:border-red-400/15 dark:hover:bg-red-500/10 dark:hover:text-red-400"
                      >
                        <span aria-hidden="true">×</span>
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {error && (
          <p role="alert" className="mt-4 px-1 text-xs leading-5 text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
      </div>
    </aside>
  );
}
