"use client";

import type { ReactNode } from "react";
import { useSession } from "@/components/auth/AuthProvider";

/**
 * Renders protected content only once the session is confirmed. While the
 * token is being checked, or while the redirect to `/login` is in flight, it
 * shows a neutral placeholder so no protected data is ever painted.
 */
export function AuthGuard({ children }: { children: ReactNode }) {
  const { status } = useSession();

  if (status === "authenticated") {
    return <>{children}</>;
  }

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col items-center justify-center px-6 py-24">
      <div
        className="h-8 w-8 animate-spin rounded-full border-2 border-[color:var(--border-soft)] border-t-[color:var(--flow-blue)]"
        aria-hidden="true"
      />
      <p role="status" className="mt-4 text-sm text-[color:var(--text-muted)]">
        {status === "loading" ? "Checking your session..." : "Redirecting to sign in..."}
      </p>
    </main>
  );
}
