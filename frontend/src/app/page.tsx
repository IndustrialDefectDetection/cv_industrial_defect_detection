"use client";
import { useState } from "react";

export default function Home() {
  const [input, setInput] = useState("");
  return (
    <main className="flex min-h-screen bg-[radial-gradient(circle_at_15%_12%,_#3b82f62e_0%,_transparent_30%),radial-gradient(circle_at_88%_18%,_#8b5cf62b_0%,_transparent_32%),radial-gradient(circle_at_65%_88%,_#14b8a626_0%,_transparent_36%),linear-gradient(135deg,_#f8fafc_0%,_#e8eef8_50%,_#eef2f7_100%)] text-slate-900">

      <aside className=" w-64 border-r border-white/60 bg-white/35 p-5 shadow-[8px_0_32px_rgba(71,85,105,0.08)] backdrop-blur-2xl">
      </aside>
      <section className="flex flex-1 relative flex-col justify-center items-center">
        <header className="mb-15 text-4xl font-semibold text-slate-800">
          Defect Detection
        </header>
        <div className="items-center">
          Messages
        </div>
        <footer className="w-[min(90%,48rem)] rounded-2xl border border-white/80 bg-white/70 p-2 text-slate-800 shadow-lg flex ">

          <div className="flex w-full items-center gap-3">
            <input
              className="flex-1 bg-transparent px-4
           placeholder:text-slate-600 outline-0"
              type="text"
              placeholder="Type here...."
              value={input ?? ""}
              onChange={(e) => setInput(e.target.value)}
            />
            {/*change "send" to icon button */}
            <button className="rounded-full justify-end bg-black/95 text-white/90 p-2 mr-1">Send</button>
          </div>

        </footer>

      </section>
    </main>
  );
}
