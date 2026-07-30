"use client";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent } from "react";
import { authClient } from "@/lib/auth-client";

type AuthMode = "signIn" | "signUp";

export default function SignInPage() {
  return (
    <Suspense fallback={null}>
      <SignInCard />
    </Suspense>
  );
}

function SignInCard() {
  const searchParams = useSearchParams();
  const [mode, setMode] = useState<AuthMode>(() =>
    searchParams.get("mode") === "signUp" ? "signUp" : "signIn"
  );
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
    const applySystemTheme = () => {
      document.documentElement.classList.toggle("dark", systemTheme.matches);
    };

    applySystemTheme();
    systemTheme.addEventListener("change", applySystemTheme);

    return () => {
      systemTheme.removeEventListener("change", applySystemTheme);
    };
  }, []);

  function selectMode(nextMode: AuthMode) {
    setMode(nextMode);
    setErrorMessage(null);
  }

  async function handleEmailSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setIsSubmitting(true);

    try {
      const result = mode === "signUp"
        ? await authClient.signUp.email({
          name,
          email,
          password,
          callbackURL: "/",
        })
        : await authClient.signIn.email({
          email,
          password,
          callbackURL: "/",
        });

      if (result.error) {
        setErrorMessage(result.error.message ?? "Authentication failed.");
        return;
      }

      window.location.assign("/");
    } catch {
      setErrorMessage("Authentication failed. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function signInWithGoogle() {
    setErrorMessage(null);

    try {
      const result = await authClient.signIn.social({
        provider: "google",
        callbackURL: "/",
      });

      if (result.error) {
        setErrorMessage(result.error.message ?? "Google authentication failed.");
      }
    } catch {
      setErrorMessage("Google authentication failed. Please try again.");
    }
  }

  return (
    <main className="flex min-h-screen bg-[radial-gradient(circle_at_top,_#dbeafe,_transparent_45%),linear-gradient(135deg,_#f8fafc,_#e8eef8)] dark:bg-[linear-gradient(145deg,_#0d0e10,_#090a0c)]">
      <div className="flex flex-1 items-center justify-center px-6 py-10">
        <section className="w-full max-w-md rounded-2xl border border-white/70 bg-white/75 p-8 shadow-[0_24px_70px_rgba(30,41,59,0.2)] backdrop-blur-2xl dark:border-[#35373d] dark:bg-[#17181c]/92 dark:shadow-[0_28px_72px_rgba(0,0,0,0.44)]">
        <h1 className="text-center text-3xl font-semibold tracking-tight text-slate-900 dark:text-zinc-200">
          Defect Detection
        </h1>

        <p className="mt-3 text-center text-sm leading-6 text-slate-600 dark:text-zinc-400">
          {mode === "signIn"
            ? "Welcome back. Sign in to continue."
            : "Create an account to save your analysis and chat history."}
        </p>

        <div className="mt-7 grid grid-cols-2 rounded-xl bg-slate-200/70 p-1 dark:bg-[#101114]/75 dark:ring-1 dark:ring-white/5">
          <button
            type="button"
            onClick={() => selectMode("signIn")}
            className={`rounded-lg px-4 py-2 text-sm font-normal transition ${mode === "signIn"
              ? "bg-white text-slate-900 shadow-sm dark:bg-[#292b31] dark:text-zinc-200 dark:shadow-[0_4px_12px_rgba(0,0,0,0.2)]"
              : "text-slate-500 hover:text-slate-800 dark:text-zinc-500 dark:hover:text-zinc-300"
              }`}
          >
            Sign in
          </button>
          <button
            type="button"
            onClick={() => selectMode("signUp")}
            className={`rounded-lg px-4 py-2 text-sm font-normal transition ${mode === "signUp"
              ? "bg-white text-slate-900 shadow-sm dark:bg-[#292b31] dark:text-zinc-200 dark:shadow-[0_4px_12px_rgba(0,0,0,0.2)]"
              : "text-slate-500 hover:text-slate-800 dark:text-zinc-500 dark:hover:text-zinc-300"
              }`}
          >
            Create account
          </button>
        </div>

        <form onSubmit={handleEmailSubmit} className="mt-6 grid gap-4 text-left">
          {mode === "signUp" && (
            <label className="grid gap-2 text-sm font-normal text-slate-700 dark:text-zinc-400">
              Name
              <input
                type="text"
                autoComplete="name"
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="rounded-xl border border-slate-300/80 bg-white/70 px-4 py-3 font-normal text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-[#383a42] dark:bg-[#111216]/70 dark:text-zinc-200 dark:placeholder:text-zinc-600 dark:focus:border-zinc-500 dark:focus:ring-zinc-500/15"
                placeholder="Your name"
              />
            </label>
          )}

          <label className="grid gap-2 text-sm font-normal text-slate-700 dark:text-zinc-400">
            Email
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="rounded-xl border border-slate-300/80 bg-white/70 px-4 py-3 font-normal text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-[#383a42] dark:bg-[#111216]/70 dark:text-zinc-200 dark:placeholder:text-zinc-600 dark:focus:border-zinc-500 dark:focus:ring-zinc-500/15"
              placeholder="you@example.com"
            />
          </label>

          <label className="grid gap-2 text-sm font-normal text-slate-700 dark:text-zinc-400">
            Password
            <input
              type="password"
              autoComplete={mode === "signUp" ? "new-password" : "current-password"}
              required
              minLength={8}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="rounded-xl border border-slate-300/80 bg-white/70 px-4 py-3 font-normal text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-[#383a42] dark:bg-[#111216]/70 dark:text-zinc-200 dark:placeholder:text-zinc-600 dark:focus:border-zinc-500 dark:focus:ring-zinc-500/15"
              placeholder="At least 8 characters"
            />
          </label>

          {errorMessage && (
            <p role="alert" className="text-sm text-red-600 dark:text-red-400">
              {errorMessage}
            </p>
          )}

          <button
            type="submit"
            disabled={isSubmitting}
            className="mt-1 rounded-xl bg-slate-900 px-5 py-3 font-normal text-white shadow-lg transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-zinc-300 dark:text-zinc-900 dark:shadow-[0_8px_22px_rgba(0,0,0,0.24)] dark:hover:bg-zinc-200"
          >
            {isSubmitting
              ? "Please wait..."
              : mode === "signIn"
                ? "Sign in"
                : "Create account"}
          </button>
        </form>

        <div className="my-6 flex items-center gap-3 text-xs font-normal text-slate-400 dark:text-zinc-600">
          <span className="h-px flex-1 bg-slate-300/80 dark:bg-[#303238]" />
          or
          <span className="h-px flex-1 bg-slate-300/80 dark:bg-[#303238]" />
        </div>

        <button
          type="button"
          onClick={signInWithGoogle}
          className="w-full rounded-xl border border-blue-500 bg-blue-600 px-5 py-3 font-normal text-white shadow-lg shadow-blue-600/20 transition hover:border-blue-400 hover:bg-blue-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-blue-500 dark:bg-blue-600 dark:text-white dark:hover:border-blue-400 dark:hover:bg-blue-500"
        >
          Continue with Google
        </button>
        </section>
      </div>
    </main>
  );
}
