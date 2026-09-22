"use client";

import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { buildLoginUrl, LOGIN_PATH, readToken, UNAUTHORIZED_EVENT } from "@/lib/auth-storage";
import { fetchCurrentUser, logout } from "@/services/auth-service";
import type { AuthenticatedUser, Profile, SessionStatus } from "@/types/auth";

type SessionContextValue = {
  status: SessionStatus;
  user: AuthenticatedUser | null;
  /** Replaces the cached profile after a successful `PUT /profiles/me`. */
  applyProfile: (profile: Profile) => void;
  signOut: () => void;
};

const SessionContext = createContext<SessionContextValue | null>(null);

/** The path the user should come back to after signing in. */
function currentPathWithQuery(): string | null {
  if (typeof window === "undefined") {
    return null;
  }

  return `${window.location.pathname}${window.location.search}`;
}

/**
 * Owns the client-side session: it validates the stored token once on mount,
 * exposes the authenticated user, and reacts to any protected call that comes
 * back 401 by sending the user to the login page.
 *
 * Mounted only under `app/(protected)`, so public pages never read the token
 * and never redirect.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [status, setStatus] = useState<SessionStatus>("loading");
  const [user, setUser] = useState<AuthenticatedUser | null>(null);
  const hasRedirected = useRef(false);

  const redirectToLogin = useCallback(() => {
    if (hasRedirected.current) {
      return;
    }

    hasRedirected.current = true;
    setStatus("unauthenticated");
    setUser(null);
    router.replace(buildLoginUrl(currentPathWithQuery()));
  }, [router]);

  useEffect(() => {
    // A protected call rejected the token while the user was on the page.
    window.addEventListener(UNAUTHORIZED_EVENT, redirectToLogin);

    return () => window.removeEventListener(UNAUTHORIZED_EVENT, redirectToLogin);
  }, [redirectToLogin]);

  useEffect(() => {
    let isCurrent = true;

    // An absent token is settled without a request; a present one is only
    // trusted once `GET /auth/me` accepts it, which also catches expiry.
    if (!readToken()) {
      redirectToLogin();
      return;
    }

    fetchCurrentUser()
      .then((currentUser) => {
        if (!isCurrent) {
          return;
        }

        setUser(currentUser);
        setStatus("authenticated");
      })
      .catch(() => {
        // `requestAuthenticatedApi` already cleared the token and dispatched
        // the unauthorized event on a 401; anything else is treated the same
        // way, because an unverified session must not render protected data.
        if (isCurrent) {
          redirectToLogin();
        }
      });

    return () => {
      isCurrent = false;
    };
  }, [redirectToLogin]);

  const applyProfile = useCallback((profile: Profile) => {
    setUser((currentUser) => (currentUser ? { ...currentUser, profile } : currentUser));
  }, []);

  const signOut = useCallback(() => {
    logout();
    hasRedirected.current = true;
    setStatus("unauthenticated");
    setUser(null);
    router.replace(LOGIN_PATH);
  }, [router]);

  const value = useMemo<SessionContextValue>(
    () => ({ status, user, applyProfile, signOut }),
    [status, user, applyProfile, signOut],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const context = useContext(SessionContext);

  if (!context) {
    throw new Error("useSession must be used inside an AuthProvider");
  }

  return context;
}
