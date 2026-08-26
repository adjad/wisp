// Frame reader for Wisp's SSE endpoints (/agent, /assistant/events via
// /sandbox/world/stream). EventSource is unusable here: it's GET-only and
// /agent is POST — so this reads the raw byte stream via fetch() +
// ReadableStream and splits on blank-line-terminated "data: {json}\n\n"
// frames, same as the real Swift client (WispClient.swift) does by hand.
export async function* sseEvents(url, init) {
  const res = await fetch(url, init);
  if (!res.ok || !res.body) {
    throw new Error(`${url} -> HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, i);
        buf = buf.slice(i + 2);
        for (const line of frame.split("\n")) {
          if (line.startsWith("data: ")) {
            try {
              yield JSON.parse(line.slice(6));
            } catch {
              // malformed frame — skip rather than kill the whole stream
            }
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
