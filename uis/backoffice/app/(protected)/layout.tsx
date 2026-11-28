import type { ReactNode } from "react";
import { AuthGuard } from "@/components/auth/AuthGuard";
import { AuthProvider } from "@/components/auth/AuthProvider";
import { BackofficeNav } from "@/components/common/BackofficeNav";
import { LiveProvider } from "@/components/live/LiveProvider";
import { OperationalAlertBanner } from "@/components/operations/OperationalAlertBanner";

/**
 * Every route in this group requires a session. The check is client-side on
 * purpose: the token lives in `localStorage`, which Next.js middleware runs too
 * early — and on the wrong side of the network — to read.
 *
 * Inside the guard, staff sessions keep one live notification stream open for every page
 * (`LiveProvider`, Milestone 10): a new RFP and an operational inactivity alert are noticed
 * wherever the team is working. The alerts banner sits under the header while any is open.
 */
export default function ProtectedLayout({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <AuthGuard>
        <LiveProvider>
          <BackofficeNav />
          <OperationalAlertBanner />
          {children}
        </LiveProvider>
      </AuthGuard>
    </AuthProvider>
  );
}
