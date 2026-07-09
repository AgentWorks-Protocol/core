"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AwMark } from "../AwMark";

const TABS = [
  { href: "/dashboard", label: "Marketplace" },
  { href: "/dashboard/new", label: "Post a job" },
  { href: "/dashboard/register", label: "Register agent" },
  { href: "/dashboard/proofs", label: "Proofs" },
  { href: "/dashboard/flow", label: "Flow" },
];

export function Shell() {
  const path = usePathname();
  const active = (href: string) => (href === "/dashboard" ? path === href : path.startsWith(href));
  return (
    <div className="bar">
      <div className="wrap">
        <Link href="/" style={{ display: "flex", alignItems: "center", gap: 8, textDecoration: "none", color: "inherit" }}>
          <AwMark size={26} style={{ color: "var(--ink)" }} />
          <span className="nm">AgentWorks</span>
        </Link>
        <nav className="tabs">
          {TABS.map((t) => (
            <Link key={t.href} href={t.href} className={`tab${active(t.href) ? " on" : ""}`}>
              {t.label}
            </Link>
          ))}
        </nav>
      </div>
    </div>
  );
}
