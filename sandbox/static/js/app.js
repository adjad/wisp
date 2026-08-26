// Bootstrap: iPhone frame chrome (status bar, clock) + a tiny hash router.
// Only #/wisp is registered so far (Phase 4) — Phase 5 adds the home screen
// and the rest of the apps; this router is what they'll plug into.
import { render as renderWispDev } from "./apps/wispdev.js";

const ROUTES = {
  "/wisp": renderWispDev,
};
const DEFAULT_ROUTE = "/wisp";

function currentPath() {
  const h = location.hash.replace(/^#/, "");
  return h || DEFAULT_ROUTE;
}

function mount() {
  const screen = document.getElementById("screen");
  const path = currentPath();
  const handler = ROUTES[path] || ROUTES[DEFAULT_ROUTE];
  handler(screen);
}

window.addEventListener("hashchange", mount);

function tickClock() {
  const clockEl = document.getElementById("statusClock");
  if (!clockEl) return;
  const now = new Date();
  clockEl.textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

async function pollDevRail() {
  const rail = document.getElementById("devRail");
  if (!rail) return;
  try {
    const res = await fetch("/sandbox/status");
    const status = await res.json();
    rail.innerHTML = "";
    const connDot = status.connected ? "ok" : "err";
    rail.appendChild(rowEl(connDot, `SSE ${status.connected ? "connected" : "disconnected"}`));
    for (const [source, s] of Object.entries(status.sync_status || {})) {
      rail.appendChild(rowEl(s.ok ? "ok" : "err",
        `${source}: ${s.count ?? 0} (${s.ok ? "ok" : s.status})`));
    }
  } catch {
    rail.innerHTML = "<div class=\"row\"><span class=\"dot err\"></span> sandbox server unreachable</div>";
  }
}

function rowEl(dotClass, text) {
  const row = document.createElement("div");
  row.className = "row";
  const dot = document.createElement("span");
  dot.className = `dot ${dotClass}`;
  const label = document.createElement("span");
  label.textContent = text;
  row.appendChild(dot);
  row.appendChild(label);
  return row;
}

mount();
tickClock();
setInterval(tickClock, 15000);
pollDevRail();
setInterval(pollDevRail, 4000);
