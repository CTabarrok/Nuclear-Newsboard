/**
 * Nuclear Newsboard — approval gateway (Cloudflare Worker, free tier).
 *
 * Receives {date, picks, token} from approve.html, verifies the token is the
 * HMAC of the date (so only people holding the emailed link can approve), and
 * triggers the repo's publish workflow via the GitHub API. The GitHub PAT
 * lives here as a Worker secret — never in the email, page, or repo.
 *
 * Setup (one time, ~5 min):
 *   1. dash.cloudflare.com → Workers & Pages → Create Worker → paste this file.
 *   2. Settings → Variables and Secrets, add three SECRETS:
 *        GH_PAT           fine-grained GitHub token, this repo only,
 *                         permission: Actions → Read and write
 *        APPROVAL_SECRET  any long random string — must match the repo
 *                         secret of the same name
 *        REPO             e.g. CTabarrok/Nuclear-Newsboard
 *   3. Copy the worker URL into WORKER_URL at the top of approve.html.
 */

const ALLOWED_ORIGIN = "*"; // optionally lock to "https://ctabarrok.github.io"

const CORS = {
  "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (request.method !== "POST") return json(405, { error: "POST only" });

    let body;
    try { body = await request.json(); }
    catch { return json(400, { error: "bad JSON" }); }

    const { date, picks, token } = body || {};
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date || "")) return json(400, { error: "bad date" });
    const nums = String(picks || "").split(",").map(Number);
    if (nums.length !== 3 || nums.some(n => !Number.isInteger(n) || n < 1 || n > 6)
        || new Set(nums).size !== 3)
      return json(400, { error: "picks must be 3 distinct numbers 1-6" });

    const expected = await hmacHex(env.APPROVAL_SECRET, date);
    if ((token || "").toLowerCase() !== expected)
      return json(403, { error: "invalid or expired token" });

    const gh = await fetch(
      `https://api.github.com/repos/${env.REPO}/actions/workflows/publish.yml/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GH_PAT}`,
          "Accept": "application/vnd.github+json",
          "User-Agent": "newsboard-approval-worker",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main", inputs: { picks: nums.join(",") } }),
      });

    if (gh.status === 204) return json(200, { ok: true });
    const detail = await gh.text();
    return json(502, { error: `GitHub API ${gh.status}: ${detail.slice(0, 200)}` });
  },
};
