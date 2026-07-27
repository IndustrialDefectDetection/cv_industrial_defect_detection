type AppSidebarProps = {
  hasMessages?: boolean;
};

export default function AppSidebar({
  hasMessages = false,
}: AppSidebarProps) {
  return (
    <aside
      className={`app-sidebar-reveal relative z-[5] flex w-[var(--app-sidebar-width)] shrink-0 flex-col overflow-hidden border-r border-slate-200/60 p-5 pt-24 backdrop-blur-2xl transition-[background-color,box-shadow] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)] dark:border-[#1b1c20] dark:bg-[linear-gradient(180deg,_#101114_0%,_#090a0c_100%)] ${hasMessages
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
  );
}
