// tunnel-provisioner -- Cloudflare Worker behind sahlisoft_platform.
//
// Holds the account-scoped Cloudflare API token so Django never has to: the
// platform authenticates here with a narrow shared secret (AUTH_TOKEN) that is
// good for nothing but "ask for a tunnel" / "tear one down".
//
// Two actions, discriminated by body.action:
//   (absent, or anything unrecognised) -> ensure   [the original behaviour]
//   "delete"                           -> tear down a tunnel
//
// Unrecognised actions deliberately fall through to "ensure" rather than
// erroring: this Worker predates the discriminator, so callers that never send
// one must keep working untouched.
//
// Source of truth: sahlisoft_platform/cloudflare/tunnel-provisioner/worker.js

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ensureTunnel(env, base, cfHeaders, body) {
  const tenantSlug = String(body.tenant_slug || "").slice(0, 80);
  const enrollmentId = String(body.enrollment_id || "").slice(0, 20);
  let tunnelId = body.tunnel_id ? String(body.tunnel_id) : "";
  if (!tenantSlug && !tunnelId) {
    return json({ error: "tenant_slug or tunnel_id required" }, 400);
  }

  if (!tunnelId) {
    const name = "shop-" + tenantSlug + (enrollmentId ? "-" + enrollmentId : "-" + Date.now());
    const createResp = await fetch(base, {
      method: "POST",
      headers: cfHeaders,
      // config_src: "cloudflare" makes this a REMOTELY MANAGED tunnel. That is
      // what lets the agent run `cloudflared service install <token>` with no
      // login, no cert.pem, no config.yml and no DNS step -- the token carries
      // the credentials. Changing this to "local" would break the agent's
      // zero-touch setup (sahlisoft_mcp/tunnel.py).
      body: JSON.stringify({ name, config_src: "cloudflare" }),
    });
    const created = await createResp.json();
    if (!createResp.ok || !created.success) {
      return json({ error: "tunnel create failed", detail: created }, 502);
    }
    tunnelId = created.result.id;
  }

  const tokenResp = await fetch(base + "/" + tunnelId + "/token", { headers: cfHeaders });
  const tokenBody = await tokenResp.json();
  if (!tokenResp.ok || !tokenBody.success) {
    return json({ error: "tunnel token fetch failed", detail: tokenBody }, 502);
  }

  return json({ tunnel_id: tunnelId, tunnel_token: tokenBody.result });
}

async function deleteTunnel(env, base, cfHeaders, body) {
  const tunnelId = String(body.tunnel_id || "");
  if (!tunnelId) {
    return json({ error: "tunnel_id required for delete" }, 400);
  }

  // A tunnel with live connections refuses to delete. Force them closed first;
  // a failure here is not fatal on its own, since the delete below is the real
  // test and may well succeed anyway (e.g. the connector was already gone).
  await fetch(base + "/" + tunnelId + "/connections", { method: "DELETE", headers: cfHeaders }).catch(() => {});

  let last = null;
  // The connector can take a moment to actually drop after connections are
  // cleaned up, so give it a few tries before declaring this retryable.
  for (let attempt = 0; attempt < 3; attempt++) {
    const resp = await fetch(base + "/" + tunnelId, { method: "DELETE", headers: cfHeaders });

    // Already gone is success, not failure: Django retries this call, and a
    // second attempt must not leave an enrollment permanently undeletable.
    if (resp.status === 404) {
      return json({ deleted: true, already_gone: true, tunnel_id: tunnelId });
    }

    const parsed = await resp.json().catch(() => ({}));
    if (resp.ok && parsed.success) {
      return json({ deleted: true, tunnel_id: tunnelId });
    }
    last = parsed;

    const alreadyDeleted = (parsed.errors || []).some(
      (e) => e.code === 1003 || /not found|already deleted/i.test(e.message || "")
    );
    if (alreadyDeleted) {
      return json({ deleted: true, already_gone: true, tunnel_id: tunnelId });
    }

    if (attempt < 2) await sleep(1000 * (attempt + 1));
  }

  // Django's contract: retryable means "leave the row alone, tell the operator
  // to try again shortly" -- never orphan the tunnel by deleting the row anyway.
  const stillConnected = (last?.errors || []).some((e) =>
    /active connection|connections/i.test(e.message || "")
  );
  return json(
    {
      deleted: false,
      retryable: stillConnected,
      error: "tunnel delete failed",
      detail: last,
    },
    502
  );
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return json({ error: "POST only" }, 405);
    }
    const auth = request.headers.get("Authorization") || "";
    const [scheme, token] = auth.split(" ");
    if (scheme !== "Bearer" || token !== env.AUTH_TOKEN) {
      return json({ error: "unauthorized" }, 401);
    }

    let body;
    try {
      body = await request.json();
    } catch (e) {
      return json({ error: "malformed JSON body" }, 400);
    }

    const cfHeaders = {
      Authorization: "Bearer " + env.CF_API_TOKEN,
      "Content-Type": "application/json",
    };
    const base = "https://api.cloudflare.com/client/v4/accounts/" + env.CF_ACCOUNT_ID + "/cfd_tunnel";

    try {
      if (String(body.action || "") === "delete") {
        return await deleteTunnel(env, base, cfHeaders, body);
      }
      return await ensureTunnel(env, base, cfHeaders, body);
    } catch (err) {
      return json({ error: "upstream request failed", detail: String(err) }, 502);
    }
  },
};
