"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Button, ErrorNote, Field, Input } from "@/components/ui";
import { login } from "@/lib/api";
import { useAuth } from "@/lib/auth";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const { reload } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      await reload();
      const next = params.get("next");
      router.replace(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-xl border border-line bg-surface p-8 shadow-sm">
      <div className="mb-2 flex items-center gap-2">
        <div className="flex size-9 items-center justify-center rounded-lg bg-accent text-lg font-bold text-white">F</div>
        <div>
          <h1 className="text-lg font-semibold">FlowForge AI</h1>
          <p className="text-xs text-ink-2">Enterprise workflow automation</p>
        </div>
      </div>
      <ErrorNote message={error} />
      <Field label="Work email">
        <Input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </Field>
      <Field label="Password">
        <Input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
      </Field>
      <Button variant="primary" className="w-full" type="submit" loading={busy}>
        Sign in
      </Button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-page p-4">
      <Suspense>
        <LoginForm />
      </Suspense>
    </div>
  );
}
