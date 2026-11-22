"use client";

import { createAvatar } from "@dicebear/core";
import * as notionists from "@dicebear/notionists";
import { useState, type FC } from "react";
import { cn } from "@/lib/utils";

/** Warm tints of the palette tokens (sand, terracotta, teal, olive, ochre). */
const BACKGROUNDS = ["f4ebdf", "f3d9cb", "d6e8e5", "e3e9d6", "f1e2c4"];

const cache = new Map<string, string>();

/** Illustrated portrait generated locally from the name (no network, so candidate names never leave the app). */
function generatedAvatar(seed: string): string {
  let uri = cache.get(seed);
  if (!uri) {
    uri = createAvatar(notionists, { seed, backgroundColor: BACKGROUNDS, backgroundType: ["solid"] }).toDataUri();
    cache.set(seed, uri);
  }
  return uri;
}

/** Real photo from the profile when present; otherwise a generated portrait. */
export function profilePhoto(profile: Record<string, unknown> | undefined | null): string | undefined {
  const v = profile?.avatar_url ?? profile?.avatarUrl ?? profile?.photo_url ?? profile?.photoUrl;
  return typeof v === "string" && v ? v : undefined;
}

type Props = { name: string; src?: string; size?: number; className?: string };

export const CandidateAvatar: FC<Props> = ({ name, src, size = 32, className }) => {
  const [failed, setFailed] = useState(false);
  const url = src && !failed ? src : generatedAvatar(name);
  return (
    // eslint-disable-next-line @next/next/no-img-element -- data URIs and arbitrary profile hosts; next/image adds nothing here
    <img
      src={url}
      alt=""
      aria-hidden
      width={size}
      height={size}
      onError={() => setFailed(true)}
      className={cn("shrink-0 rounded-full object-cover ring-1 ring-line", className)}
      style={{ width: size, height: size }}
    />
  );
};
