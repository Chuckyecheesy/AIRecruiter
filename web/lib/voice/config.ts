/**
 * Voice intake config, read from public env vars (see lib/voice/INTAKE_AGENT.md).
 *   NEXT_PUBLIC_ELEVENLABS_INTAKE_AGENT_ID  public agent ID (no server needed)
 *   NEXT_PUBLIC_ELEVENLABS_TOKEN_URL        optional server route returning a conversation token / signed URL
 *   NEXT_PUBLIC_VOICE_MODE=mock             force the scripted mock even when an agent is configured
 */
export function voiceConfig() {
  const agentId = process.env.NEXT_PUBLIC_ELEVENLABS_INTAKE_AGENT_ID || "";
  const tokenUrl = process.env.NEXT_PUBLIC_ELEVENLABS_TOKEN_URL || "";
  const forceMock = process.env.NEXT_PUBLIC_VOICE_MODE === "mock";
  const mode: "mock" | "elevenlabs" = !forceMock && (agentId || tokenUrl) ? "elevenlabs" : "mock";
  return { agentId, tokenUrl, mode };
}
