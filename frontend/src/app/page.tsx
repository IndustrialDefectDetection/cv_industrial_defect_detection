"use client";
import { useState } from "react";

type Message = {
    id: number;
    text: string;
    role: "user" | "assistant";
  }

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const hasMessages = messages.length > 0;
  const isLoading = messages[messages.length - 1]?.role === "user";

  function handleSend() {
    if(input.trim() !== "") {
      const newMessage: Message = {
        id: messages.length + 1,
        text: input,
        role: "user"
      };
      setMessages([...messages, newMessage]);
      setInput("");
      getResponse(newMessage.text);
    }
    return;
  }
  function getResponse(userInput: string) {

  }

  return (
    <main className="relative flex min-h-screen bg-[radial-gradient(circle_at_15%_12%,_#3b82f62e_0%,_transparent_30%),radial-gradient(circle_at_88%_18%,_#8b5cf62b_0%,_transparent_32%),radial-gradient(circle_at_65%_88%,_#14b8a626_0%,_transparent_36%),linear-gradient(135deg,_#f8fafc_0%,_#e8eef8_50%,_#eef2f7_100%)] text-slate-900">

      <div
        aria-hidden="true"
        className={`absolute inset-x-0 top-0 z-10 h-[4.5rem] border-b border-slate-200/60 bg-[linear-gradient(90deg,rgba(255,255,255,0.72),rgba(248,250,252,0.52),rgba(255,255,255,0.64))] backdrop-blur-2xl transition-[opacity,transform,box-shadow] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] ${
          hasMessages
            ? "translate-y-0 opacity-100 shadow-[0_10px_32px_rgba(71,85,105,0.09)]"
            : "-translate-y-3 opacity-0 shadow-none"
        }`}
      />

      <header
        className={`absolute z-20 flex items-center gap-2.5 whitespace-nowrap font-semibold tracking-tight text-slate-800 transition-all duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] ${
          hasMessages
            ? "left-5 top-[1.125rem] translate-x-0 text-2xl"
            : "left-[calc(50%+8rem)] top-[calc(50%-6rem)] -translate-x-1/2 text-4xl"
        }`}
      >
        <span
          aria-hidden="true"
          className={`h-2.5 w-2.5 rounded-full bg-blue-500 shadow-[0_0_14px_rgba(59,130,246,0.65)] transition-[opacity,transform] delay-150 duration-300 ${
            hasMessages ? "scale-100 opacity-100" : "scale-0 opacity-0"
          }`}
        />
        <span>Defect Detection</span>
      </header>

      <aside
        className={`relative z-[5] w-64 shrink-0 overflow-hidden border-r border-slate-200/60 p-5 pt-24 backdrop-blur-2xl transition-[background-color,box-shadow] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)] ${
          hasMessages
            ? "bg-white/38 shadow-[10px_0_36px_rgba(71,85,105,0.09)]"
            : "bg-white/28 shadow-[8px_0_30px_rgba(71,85,105,0.07)]"
        }`}
      >
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-4 top-[4.5rem] h-px bg-gradient-to-r from-transparent via-slate-300/70 to-transparent"
        />
        <div
          aria-hidden="true"
          className={`pointer-events-none absolute -left-20 top-24 h-56 w-56 rounded-full bg-blue-400/10 blur-3xl transition-opacity duration-700 ${
            hasMessages ? "opacity-100" : "opacity-60"
          }`}
        />
      </aside>
      <section className="flex flex-1 relative flex-col justify-center items-center">
        {/*User and assistant messages*/}
        <div
          className={`absolute left-1/2 flex max-h-[calc(100vh-12rem)] w-[min(90%,48rem)] -translate-x-1/2 flex-col overflow-y-auto px-1 transition-[top] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] ${
            hasMessages ? "top-24" : "top-1/2"
          }`}
        >
          {messages.map((message) => (
            message.role === "user" ? (
              <div key={message.id} className="message-enter-user mb-4 rounded-lg bg-black/95 p-2 text-white shadow-lg flex ml-auto justify-end w-fit max-w-[70%] pr-3 pl-3">
                {message.text}
              </div>
            ) : (
              <div key={message.id} className="message-enter-assistant mb-4 rounded-lg bg-white/70 p-2 text-slate-800 shadow-lg flex mr-auto justify-start w-fit max-w-[70%] pr-3 pl-3">
                {message.text}
              </div>
            )
          ))}
          {isLoading && (
            <div
              role="status"
              aria-label="Waiting for assistant response"
              className="mb-4 mr-auto h-1.5 w-32 overflow-hidden rounded-full bg-slate-300/60 shadow-inner"
            >
              <div className="assistant-loading-bar h-full w-1/2 rounded-full bg-gradient-to-r from-blue-500 via-violet-500 to-cyan-400" />
            </div>
          )}
        </div>
        <footer
          className={`absolute left-1/2 z-[6] flex w-[min(90%,48rem)] -translate-x-1/2 rounded-2xl border border-white/80 bg-white/70 p-2 text-slate-800 shadow-lg backdrop-blur-xl transition-[top,box-shadow] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] ${
            hasMessages
              ? "top-[calc(100%-5.5rem)] shadow-[0_14px_36px_rgba(71,85,105,0.16)]"
              : "top-1/2"
          }`}
        >
          <div className="flex w-full items-center gap-3">
            <input
              className="flex-1 bg-transparent px-4
           placeholder:text-slate-600 outline-0"
              type="text"
              placeholder="Type here...."
              value={input ?? ""}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if(e.key == "Enter"){
                  handleSend()
                }
              }}
            />
            {/*change "send" to icon button */}
            <button className="rounded-full  bg-black/95 text-white/90 p-2"
            onClick={handleSend}>Send</button>
          </div>

        </footer>

      </section>
    </main>
  );
}
