"use client";
import { shouldUseReactServerCondition } from "next/dist/build/utils";
import { useEffect, useRef, useState } from "react";

type Message = {
  id: number;
  text: string;
  role: "user" | "assistant";
}

type ThemeMode = "light" | "dark" | "system";
export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const hasMessages = messages.length > 0;
  const [isWaiting, setIsWaiting] = useState(false);
  const [themeMode, setThemeMode] = useState<ThemeMode>("system")
  const messagesEndRef = useRef<HTMLDivElement>(null);

  //System theme handling
  useEffect(() => {
    const systemTheme = window.matchMedia("(prefers-color-scheme: dark");
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
    // Scroll to end of messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [messages, isWaiting]);

  async function handleSend() {
    if (input.trim() !== "") {
      const newMessage: Message = {
        id: messages.length + 1,
        text: input,
        role: "user"
      };
      setMessages([...messages, newMessage]);
      setInput("");
      setIsWaiting(true);
      try {
        const response = await getResponse(newMessage.text);
        const chatResponse: Message = {
          id: newMessage.id + 1,
          text: response.analysis,
          role: "assistant"
        }
        setMessages(currentMessages => [...currentMessages, chatResponse]);
      } finally {
        setIsWaiting(false)
      }
    }
  }
  async function getResponse(userInput: string) {
    const response = await fetch(
      "http://127.0.0.1:8000/chat/",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          "user_input": userInput
        })
      }
    )
    const data = await response.json()
    return data
  }

  return (
    <main className="relative flex min-h-screen bg-[radial-gradient(circle_at_15%_12%,_#3b82f62e_0%,_transparent_30%),radial-gradient(circle_at_88%_18%,_#8b5cf62b_0%,_transparent_32%),radial-gradient(circle_at_65%_88%,_#14b8a626_0%,_transparent_36%),linear-gradient(135deg,_#f8fafc_0%,_#e8eef8_50%,_#eef2f7_100%)] text-slate-900 dark:bg-[linear-gradient(145deg,_#0d0e10_0%,_#090a0c_55%,_#0c0d0f_100%)] dark:text-zinc-100">

      <div
        aria-hidden="true"
        className={`absolute inset-x-0 top-0 z-10 h-[4.5rem] border-b border-slate-200/60 bg-[linear-gradient(90deg,rgba(255,255,255,0.72),rgba(248,250,252,0.52),rgba(255,255,255,0.64))] backdrop-blur-2xl transition-[opacity,transform,box-shadow] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] dark:border-[#24262b] dark:bg-[linear-gradient(90deg,rgba(12,13,15,0.97),rgba(7,8,10,0.94),rgba(12,13,15,0.96))] ${hasMessages
            ? "translate-y-0 opacity-100 shadow-[0_10px_32px_rgba(71,85,105,0.09)]"
            : "-translate-y-3 opacity-0 shadow-none"
          }`}
      />

      <header
        className={`absolute z-20 flex items-center gap-2.5 whitespace-nowrap font-semibold tracking-tight text-slate-800 transition-all duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] dark:text-slate-100 ${hasMessages
            ? "left-5 top-[1.125rem] translate-x-0 text-2xl"
            : "left-[calc(50%+8rem)] top-[calc(50%-6rem)] -translate-x-1/2 text-4xl"
          }`}
      >
        <span
          aria-hidden="true"
          className={`h-2.5 w-2.5 rounded-full bg-blue-500 shadow-[0_0_14px_rgba(59,130,246,0.65)] transition-[opacity,transform] delay-150 duration-300 dark:bg-zinc-300 dark:shadow-[0_0_14px_rgba(212,212,216,0.45)] ${hasMessages ? "scale-100 opacity-100" : "scale-0 opacity-0"
            }`}
        />
        <span>Defect Detection</span>
      </header>

      <div className="absolute right-5 top-[0.9rem] z-20 w-56">
        <fieldset className="theme-switch">
          <legend className="sr-only">Color theme</legend>

          <input
            id="theme-light"
            name="theme"
            type="radio"
            checked={themeMode === "light"}
            onChange={() => setThemeMode("light")}
          />
          <label htmlFor="theme-light">Light</label>

          <input
            id="theme-dark"
            name="theme"
            type="radio"
            checked={themeMode === "dark"}
            onChange={() => setThemeMode("dark")}
          />
          <label htmlFor="theme-dark">Dark</label>

          <input
            id="theme-system"
            name="theme"
            type="radio"
            checked={themeMode === "system"}
            onChange={() => setThemeMode("system")}
          />
          <label htmlFor="theme-system">Auto</label>
        </fieldset>
      </div>

      <aside
        className={`relative z-[5] flex w-64 shrink-0 flex-col overflow-hidden border-r border-slate-200/60 p-5 pt-24 backdrop-blur-2xl transition-[background-color,box-shadow] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)] dark:border-[#1b1c20] dark:bg-[linear-gradient(180deg,_#101114_0%,_#090a0c_100%)] ${hasMessages
            ? "bg-white/38 shadow-[10px_0_36px_rgba(71,85,105,0.09)] dark:shadow-[8px_0_24px_rgba(0,0,0,0.18)]"
            : "bg-white/28 shadow-[8px_0_30px_rgba(71,85,105,0.07)] dark:shadow-[6px_0_20px_rgba(0,0,0,0.14)]"
          }`}
      >
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-4 top-[4.5rem] h-px bg-gradient-to-r from-transparent via-slate-300/70 to-transparent dark:via-zinc-800/50"
        />
        <div
          aria-hidden="true"
          className={`pointer-events-none absolute -left-20 top-24 h-56 w-56 rounded-full bg-blue-400/10 blur-3xl transition-opacity duration-700 dark:bg-transparent ${hasMessages ? "opacity-100" : "opacity-60"
            }`}
        />
      </aside>
      <section className="flex flex-1 relative flex-col justify-center items-center">
        {/*User and assistant messages*/}
        <div
          className={`absolute left-1/2 flex max-h-[calc(100vh-12rem)] w-[min(92%,64rem)] -translate-x-1/2 flex-col overflow-y-auto py-1 pl-1 pr-4 [scrollbar-gutter:stable] transition-[top] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] ${hasMessages ? "top-24" : "top-1/2"
            }`}
        >
          {messages.map((message) => (
            message.role === "user" ? (
              <div key={message.id} className="message-enter-user mb-4 rounded-lg bg-black/95 p-2 text-white shadow-lg flex ml-auto justify-end w-fit max-w-[70%] pr-3 pl-3 dark:bg-zinc-800 dark:text-zinc-100 dark:shadow-[0_8px_20px_rgba(0,0,0,0.28)]">
                {message.text}
              </div>
            ) : (
              <div key={message.id} className="message-enter-assistant mb-9 mr-auto min-w-0 w-full px-2 pb-2">
                <div className="mb-3 text-sm font-normal text-slate-500 dark:text-zinc-400">
                  Analysis
                </div>
                <div className="whitespace-pre-wrap [overflow-wrap:anywhere] text-left text-base leading-7 text-slate-800 dark:text-zinc-200">
                  {message.text}
                </div>
              </div>
            )
          ))}
          {isWaiting && (
            <div
              role="status"
              aria-label="Waiting for assistant response"
              className="mb-6 mr-auto px-2 py-1"
            >
              <div aria-hidden="true" className="generating-loader">
                <div className="generating-loader__spinner" />
                <div className="generating-loader__letters">
                  {"Generating . . .".split("").map((character, index) => (
                    <span
                      key={`${character}-${index}`}
                      className="generating-loader__letter"
                      style={{ animationDelay: `${index * 0.12}s` }}
                    >
                      {character === " " ? "\u00A0" : character}
                    </span>
                  ))}
                </div>
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
              placeholder="Type here...."
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
              aria-label="Send message"
              disabled={isWaiting}
              className="rounded-full bg-black/95 p-2 text-white/90 transition-colors disabled:cursor-not-allowed disabled:bg-slate-400 disabled:text-white/70 dark:bg-zinc-100 dark:text-zinc-950 dark:disabled:bg-zinc-700 dark:disabled:text-zinc-400"
              onClick={handleSend}
            >
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
            </button>
          </div>

        </footer>

      </section>
    </main>
  );
}
