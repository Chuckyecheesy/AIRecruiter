import Link from "next/link";
import type { FC, ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Wordmark: a terracotta dot and the name in the display face. */
export const Wordmark: FC<{ className?: string }> = ({ className }) => (
  <Link href="/" className={cn("font-display flex items-center gap-2 text-[17px] font-semibold tracking-tight text-ink", className)}>
    <span aria-hidden className="size-2.5 rounded-full bg-gate" />
    AIRecruiter
  </Link>
);

export const TopBar: FC<{ children?: ReactNode }> = ({ children }) => (
  <header className="flex items-center gap-4 px-4 py-4 sm:px-8">
    <Wordmark />
    {children}
  </header>
);
