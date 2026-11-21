import { test } from "node:test";
import assert from "node:assert/strict";

import worker, { patchOriginal, verifySignature } from "./interactions.js";

const encoder = new TextEncoder();

function nowStamp() {
  return String(Math.floor(Date.now() / 1000));
}

function toHex(buffer) {
  return [...new Uint8Array(buffer)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function keypair() {
  const pair = await crypto.subtle.generateKey(
    { name: "Ed25519" },
    true,
    ["sign", "verify"],
  );
  const raw = await crypto.subtle.exportKey("raw", pair.publicKey);
  return { pair, publicKeyHex: toHex(raw) };
}

async function sign(pair, timestamp, body) {
  const data = new Uint8Array([
    ...encoder.encode(timestamp),
    ...new Uint8Array(body),
  ]);
  return toHex(await crypto.subtle.sign({ name: "Ed25519" }, pair.privateKey, data));
}

function interactionRequest(body, signature, timestamp) {
  return new Request("https://interactions.example.com/", {
    method: "POST",
    headers: {
      "X-Signature-Ed25519": signature,
      "X-Signature-Timestamp": timestamp,
    },
    body,
  });
}

function fakeCtx() {
  const pending = [];
  return {
    pending,
    waitUntil(promise) {
      pending.push(promise);
    },
  };
}

function withFetch(handler, fn) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  return Promise.resolve()
    .then(fn)
    .finally(() => {
      globalThis.fetch = original;
    });
}

test("verifySignature accepts a correctly signed body", async () => {
  const { pair, publicKeyHex } = await keypair();
  const body = encoder.encode('{"type":1}');
  const timestamp = nowStamp();
  const signature = await sign(pair, timestamp, body);

  assert.equal(await verifySignature(publicKeyHex, timestamp, body, signature), true);
  assert.equal(await verifySignature(publicKeyHex, timestamp, body, "00".repeat(64)), false);
  assert.equal(await verifySignature(publicKeyHex, timestamp, body, ""), false);
});

test("PING returns type 1 without forwarding", async () => {
  const { pair, publicKeyHex } = await keypair();
  const body = encoder.encode('{"type":1}');
  const timestamp = nowStamp();
  const signature = await sign(pair, timestamp, body);
  const ctx = fakeCtx();

  const response = await withFetch(
    () => {
      throw new Error("PING must not call fetch");
    },
    () => worker.fetch(interactionRequest(body, signature, timestamp), {
      DISCORD_PUBLIC_KEY: publicKeyHex,
    }, ctx),
  );

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { type: 1 });
  assert.equal(ctx.pending.length, 0);
});

test("APPLICATION_COMMAND defers immediately and forwards the raw body", async () => {
  const { pair, publicKeyHex } = await keypair();
  const payload = {
    type: 2,
    token: "interaction-token",
    data: { name: "setup" },
    guild_id: "123",
  };
  const body = encoder.encode(JSON.stringify(payload));
  const timestamp = nowStamp();
  const signature = await sign(pair, timestamp, body);
  const ctx = fakeCtx();
  const calls = [];

  const response = await withFetch(
    async (url, init) => {
      calls.push({ url, init });
      return new Response("ok", { status: 200 });
    },
    () => worker.fetch(interactionRequest(body, signature, timestamp), {
      DISCORD_PUBLIC_KEY: publicKeyHex,
      AZURE_DEFERRED_URL: "https://azure.example.com/api/interactions/deferred",
    }, ctx),
  );

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { type: 5, data: { flags: 64 } });
  assert.equal(ctx.pending.length, 1);
  await ctx.pending[0];

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://azure.example.com/api/interactions/deferred");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["X-Signature-Ed25519"], signature);
  assert.equal(calls[0].init.headers["X-Signature-Timestamp"], timestamp);
  assert.deepEqual(new Uint8Array(calls[0].init.body), new Uint8Array(body));
});

test("failed signature returns 401", async () => {
  const { publicKeyHex } = await keypair();
  const body = encoder.encode('{"type":1}');
  const response = await worker.fetch(
    interactionRequest(body, "00".repeat(64), nowStamp()),
    { DISCORD_PUBLIC_KEY: publicKeyHex },
    fakeCtx(),
  );
  assert.equal(response.status, 401);
});

test("stale signature is rejected", async () => {
  const { pair, publicKeyHex } = await keypair();
  const body = encoder.encode('{"type":1}');
  const stale = String(Math.floor(Date.now() / 1000) - 3600);
  const signature = await sign(pair, stale, body);
  const response = await worker.fetch(
    interactionRequest(body, signature, stale),
    { DISCORD_PUBLIC_KEY: publicKeyHex },
    fakeCtx(),
  );
  assert.equal(response.status, 401);
});

test("forward failure patches an error message to Discord", async () => {
  const { pair, publicKeyHex } = await keypair();
  const body = encoder.encode(JSON.stringify({ type: 2, token: "tok" }));
  const timestamp = nowStamp();
  const signature = await sign(pair, timestamp, body);
  const ctx = fakeCtx();
  const calls = [];

  await withFetch(
    async (url, init) => {
      calls.push({ url, init });
      if (url.includes("azure")) return new Response("boom", { status: 500 });
      return new Response("ok", { status: 200 });
    },
    async () => {
      const response = await worker.fetch(interactionRequest(body, signature, timestamp), {
        DISCORD_PUBLIC_KEY: publicKeyHex,
        DISCORD_APPLICATION_ID: "app-id",
        AZURE_DEFERRED_URL: "https://azure.example.com/api/interactions/deferred",
      }, ctx);
      assert.equal(response.status, 200);
      await ctx.pending[0];
    },
  );

  assert.equal(calls.length, 2);
  assert.ok(calls[1].url.includes("/webhooks/app-id/tok/messages/@original"));
  assert.equal(calls[1].init.method, "PATCH");
  assert.match(JSON.parse(calls[1].init.body).content, /設定処理に失敗しました/);
});

test("PATCH is retried once when Discord rejects it", async () => {
  const { pair, publicKeyHex } = await keypair();
  const body = encoder.encode(JSON.stringify({ type: 2, token: "tok" }));
  const timestamp = nowStamp();
  const signature = await sign(pair, timestamp, body);
  const ctx = fakeCtx();
  const urls = [];

  await withFetch(
    async (url) => {
      urls.push(String(url));
      return new Response("fail", { status: 500 });
    },
    async () => {
      await worker.fetch(interactionRequest(body, signature, timestamp), {
        DISCORD_PUBLIC_KEY: publicKeyHex,
        DISCORD_APPLICATION_ID: "app-id",
        AZURE_DEFERRED_URL: "https://azure.example.com/api/interactions/deferred",
      }, ctx);
      await ctx.pending[0];
    },
  );

  // Azure 1回 + PATCH 2回
  assert.equal(urls.length, 3);
  assert.equal(urls.filter((u) => u.includes("messages/@original")).length, 2);
});

test("permanent Azure config errors tell the user to contact an admin", async () => {
  const { pair, publicKeyHex } = await keypair();
  const body = encoder.encode(JSON.stringify({ type: 2, token: "tok" }));
  const timestamp = nowStamp();
  const signature = await sign(pair, timestamp, body);
  const ctx = fakeCtx();
  let patchBody;

  await withFetch(
    async (url, init) => {
      if (String(url).includes("azure")) return new Response("no key", { status: 503 });
      patchBody = JSON.parse(init.body);
      return new Response("ok", { status: 200 });
    },
    async () => {
      await worker.fetch(interactionRequest(body, signature, timestamp), {
        DISCORD_PUBLIC_KEY: publicKeyHex,
        DISCORD_APPLICATION_ID: "app-id",
        AZURE_DEFERRED_URL: "https://azure.example.com/api/interactions/deferred",
      }, ctx);
      await ctx.pending[0];
    },
  );

  assert.match(patchBody.content, /管理者に連絡してください/);
});

test("saved-but-display-failed keeps the success message", async () => {
  const { pair, publicKeyHex } = await keypair();
  const body = encoder.encode(JSON.stringify({ type: 2, token: "tok" }));
  const timestamp = nowStamp();
  const signature = await sign(pair, timestamp, body);
  const ctx = fakeCtx();
  let patchBody;

  await withFetch(
    async (url, init) => {
      if (String(url).includes("azure")) {
        return new Response("failed to edit response", {
          status: 409,
          headers: { "X-Setup-Saved": "true" },
        });
      }
      patchBody = JSON.parse(init.body);
      return new Response("ok", { status: 200 });
    },
    async () => {
      await worker.fetch(interactionRequest(body, signature, timestamp), {
        DISCORD_PUBLIC_KEY: publicKeyHex,
        DISCORD_APPLICATION_ID: "app-id",
        AZURE_DEFERRED_URL: "https://azure.example.com/api/interactions/deferred",
      }, ctx);
      await ctx.pending[0];
    },
  );

  assert.match(patchBody.content, /設定は保存されました/);
  assert.doesNotMatch(patchBody.content, /失敗しました。しばらく/);
});

test("unsupported interaction types are rejected without deferring", async () => {
  const { pair, publicKeyHex } = await keypair();
  const body = encoder.encode(JSON.stringify({ type: 4, token: "tok" }));
  const timestamp = nowStamp();
  const signature = await sign(pair, timestamp, body);
  const ctx = fakeCtx();

  const response = await worker.fetch(
    interactionRequest(body, signature, timestamp),
    { DISCORD_PUBLIC_KEY: publicKeyHex },
    ctx,
  );
  assert.equal(response.status, 400);
  assert.equal(ctx.pending.length, 0);
});

test("patchOriginal sends ephemeral-safe content", async () => {
  let seen;
  await withFetch(
    async (url, init) => {
      seen = { url, init };
      return new Response("ok", { status: 200 });
    },
    () => patchOriginal({ DISCORD_APPLICATION_ID: "app" }, "tok", "hello"),
  );
  assert.equal(seen.url, "https://discord.com/api/v10/webhooks/app/tok/messages/@original");
  assert.deepEqual(JSON.parse(seen.init.body), { content: "hello" });
});
