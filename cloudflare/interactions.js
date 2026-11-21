/**
 * Discord Interactions の即 ACK プロキシ。
 *
 * Discord は 3 秒以内の応答を要求する。Azure Functions をスケールゼロで
 * 待たせるとインスタンス確保の待ちがそれを超えるため、ここで署名検証と
 * type 5（defer）だけをミリ秒単位で返し、重い処理は ctx.waitUntil で
 * Azure の /api/interactions/deferred へバトンタッチする。
 *
 * Worker には Bot トークンを置かない。状態・権限ロジックは Python 側の
 * 1 箇所に残す。Azure への転送が失敗したときだけ、この Worker が
 * インタラクショントークンでエラー本文を PATCH する。
 */

const TEXT_ENCODER = new TextEncoder();
const TEXT_DECODER = new TextDecoder();

function hexToBytes(hex) {
  if (typeof hex !== "string" || hex.length % 2 !== 0) {
    throw new Error("invalid hex string");
  }
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i += 1) {
    const byte = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    if (Number.isNaN(byte)) throw new Error("invalid hex string");
    bytes[i] = byte;
  }
  return bytes;
}

function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

/** Discord の Ed25519 署名: sign(timestamp || rawBody) */
export async function verifySignature(publicKeyHex, timestamp, rawBody, signatureHex) {
  if (!publicKeyHex || !timestamp || !signatureHex) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      hexToBytes(publicKeyHex),
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    const data = concatBytes(TEXT_ENCODER.encode(timestamp), new Uint8Array(rawBody));
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      hexToBytes(signatureHex),
      data,
    );
  } catch {
    return false;
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** deferred メッセージ（考え中…）を結果本文へ差し替える。 */
export async function patchOriginal(env, token, content) {
  if (!token || !env.DISCORD_APPLICATION_ID) return false;
  const response = await fetch(
    `https://discord.com/api/v10/webhooks/${env.DISCORD_APPLICATION_ID}/${token}/messages/@original`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content: content.slice(0, 2000) }),
    },
  );
  return response.ok;
}

async function forward(env, rawBody, signature, timestamp, token) {
  try {
    const response = await fetch(env.AZURE_DEFERRED_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": timestamp,
      },
      body: rawBody,
    });
    if (!response.ok) {
      console.error("Azure deferred returned", response.status);
      await patchOriginal(
        env,
        token,
        `⚠️ 設定処理に失敗しました (HTTP ${response.status})。しばらくしてからもう一度お試しください。`,
      );
    }
  } catch (error) {
    console.error("Azure deferred failed", error);
    await patchOriginal(
      env,
      token,
      "⚠️ 設定処理に失敗しました。しばらくしてからもう一度お試しください。",
    );
  }
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return json({ error: "method not allowed" }, 405);
    }

    const rawBody = await request.arrayBuffer();
    const signature = request.headers.get("X-Signature-Ed25519") || "";
    const timestamp = request.headers.get("X-Signature-Timestamp") || "";

    const valid = await verifySignature(
      env.DISCORD_PUBLIC_KEY,
      timestamp,
      rawBody,
      signature,
    );
    if (!valid) {
      return json({ error: "invalid request signature" }, 401);
    }

    let payload;
    try {
      payload = JSON.parse(TEXT_DECODER.decode(rawBody));
    } catch {
      return json({ error: "invalid json" }, 400);
    }

    // Discord のエンドポイント検証（PING）
    if (payload.type === 1) {
      return json({ type: 1 });
    }

    // それ以外は「考え中…」を即返し、実処理を Azure へ回す
    if (payload.type === 2) {
      ctx.waitUntil(
        forward(env, rawBody, signature, timestamp, String(payload.token || "")),
      );
      return json({ type: 5, data: { flags: 64 } });
    }

    ctx.waitUntil(
      patchOriginal(env, String(payload.token || ""), "⚠️ 未対応のインタラクションです。"),
    );
    return json({ type: 5, data: { flags: 64 } });
  },
};
