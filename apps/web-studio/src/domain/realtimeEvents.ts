export type StreamFrame = {
  id: string;
  event: string;
  data: string;
};

/**
 * Parse one SSE frame (already split on a blank line). Comment-only frames,
 * such as the keep-alive the server emits while idle, return null so callers
 * never treat a heartbeat as an event.
 */
export function parseSseFrame(block: string): StreamFrame | null {
  let id = "";
  let event = "message";
  const data: string[] = [];
  for (const rawLine of block.split("\n")) {
    const line = rawLine.replace(/\r$/, "");
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = value;
    else if (field === "event") event = value || "message";
    else if (field === "data") data.push(value);
  }
  if (data.length === 0) return null;
  return { id, event, data: data.join("\n") };
}

/**
 * Split a streaming buffer into complete SSE frames and keep the trailing
 * partial frame for the next chunk. CRLF and LF framing are both accepted.
 */
export function drainSseFrames(buffer: string): { frames: StreamFrame[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const frames: StreamFrame[] = [];
  let rest = normalized;
  let boundary = rest.indexOf("\n\n");
  while (boundary !== -1) {
    const block = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);
    const frame = parseSseFrame(block);
    if (frame) frames.push(frame);
    boundary = rest.indexOf("\n\n");
  }
  return { frames, rest };
}
