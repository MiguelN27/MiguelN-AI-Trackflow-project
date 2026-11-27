import type { ReactNode } from "react";
import { AuthGuard } from "@/components/auth/AuthGuard";
import { AuthProvider } from "@/components/auth/AuthProvider";
import { BackofficeNav } from "@/components/common/BackofficeNav";
import { RfpLiveProvider } from "@/components/rfp/RfpLiveProvider";

/**
 * Every route in this group requires a session. The check is client-side on
 * purpose: the token lives in `localStorage`, which Next.js middleware runs too
 * early — and on the wrong side of the network — to read.
 *
 * Inside the guard, staff sessions keep one live RFP notification stream open for every page
 * (`RfpLiveProvider`, Milestone 10), so a new RFP is noticed wherever the team is working.
 */
export default function ProtectedLayout({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <AuthGuard>
        <RfpLiveProvider>
          <BackofficeNav />
          {children}
        </RfpLiveProvider>
      </AuthGuard>
    </AuthProvider>
  );
}
