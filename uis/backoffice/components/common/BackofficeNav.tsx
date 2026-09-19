"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/suppliers", label: "Suppliers" },
] as const;

function isActiveLink(pathname: string, href: string): boolean {
  if (href === "/") {
    return pathname === "/";
  }

  return pathname === href || pathname.startsWith(`${href}/`);
}

export function BackofficeNav() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-30 border-b border-[color:var(--border-soft)] bg-[color:var(--surface)]/85 backdrop-blur">
      <nav
        aria-label="Backoffice"
        className="mx-auto flex w-full max-w-6xl items-center justify-between gap-4 px-6 py-3 md:px-10"
      >
        <Link href="/" className="flex flex-col" aria-label="TrackFlow backoffice home">
          <span className="font-mono text-[0.65rem] uppercase tracking-[0.22em] text-[color:var(--brand-primary)]">
            TrackFlow
          </span>
          <span className="text-base font-semibold leading-tight text-[color:var(--foreground)]">Backoffice</span>
        </Link>

        <ul className="flex items-center gap-1">
          {NAV_LINKS.map((link) => {
            const isActive = isActiveLink(pathname, link.href);

            return (
              <li key={link.href}>
                <Link
                  href={link.href}
                  aria-current={isActive ? "page" : undefined}
                  className={`block rounded-xl px-3 py-2 text-sm font-semibold transition ${
                    isActive
                      ? "bg-[color:var(--brand-primary)]/10 text-[color:var(--brand-primary)]"
                      : "text-slate-600 hover:bg-[color:var(--brand-primary)]/5 hover:text-[color:var(--brand-primary)]"
                  }`}
                >
                  {link.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </header>
  );
}
