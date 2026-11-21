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
  } catch (error) {
    // 署名不正と公開鍵の設定ミスを区別できるよう、こちらは記録する
    console.error("signature verification error (public key misconfigured?)", error);
    return false;
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function logFailure(token, message, detail) {
  // トークンは相関 ID。秘密ではないが、全文を外部ログへ流さないよう末尾だけ使う。
  console.error(`[setup][${token.slice(-8) || "-"}] ${message}`, detail ?? "");
}

/** deferred メッセージ（考え中…）を結果本文へ差し替える。 */
export async function patchOriginal(env, token, content) {
  if (!token || !env.DISCORD_APPLICATION_ID) {
    logFailure(token, "PATCH に必要な設定がありません", {
      token: Boolean(token),
      applicationId: Boolean(env.DISCORD_APPLICATION_ID),
    });
    return false;
  }
  try {
    const response = await fetch(
      `https://discord.com/api/v10/webhooks/${env.DISCORD_APPLICATION_ID}/${token}/messages/@original`,
      {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ content: content.slice(0, 2000) }),
      },
    );
    if (!response.ok) {
      logFailure(token, "Discord への PATCH が失敗", response.status);
    }
    return response.ok;
  } catch (error) {
    logFailure(token, "Discord への PATCH が例外", error);
    return false;
  }
}

/** PATCH 自体が失敗しても「考え中…」で放置しないよう、もう一度だけ試す。 */
async function patchWithRetry(env, token, content) {
  if (await patchOriginal(env, token, content)) return true;
  if (await patchOriginal(env, token, content)) return true;
  logFailure(token, "PATCH を2回とも失敗しました（ユーザーは考え中…のまま）");
  return false;
}

function errorMessage(status) {
  // 401/404/503 は再試行で直らないオペレーター要対応の設定不備
  if ([401, 403, 404, 503].includes(status)) {
    return "⚠️ 設定処理に失敗しました（サーバー側の設定が未完了です）。管理者に連絡してください。";
  }
  return "⚠️ 設定処理に失敗しました。しばらくしてからもう一度お試しください。";
}

async function forward(env, rawBody, signature, timestamp, token) {
  try {
    // waitUntil の 30 秒制限より前に打ち切り、フォールバック PATCH を必ず試す
    const response = await fetch(env.AZURE_DEFERRED_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": timestamp,
      },
      body: rawBody,
      signal: AbortSignal.timeout(20000),
    });
    if (!response.ok) {
      logFailure(token, "Azure deferred returned", response.status);
      // 409 = 設定は保存済みで表示更新だけ失敗した場合
      const saved = response.headers.get("X-Setup-Saved") === "true";
      await patchWithRetry(
        env,
        token,
        saved
          ? "✅ 設定は保存されました"
          : errorMessage(response.status),
      );
    }
  } catch (error) {
    logFailure(token, "Azure deferred failed", error);
    await patchWithRetry(env, token, errorMessage(0));
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

    // 署名は有効でも、古いインタラクションのリプレイは拒否する
    const issuedAt = Number(timestamp);
    if (
      !Number.isFinite(issuedAt) ||
      Math.abs(Date.now() / 1000 - issuedAt) > 300
    ) {
      console.error("stale signature rejected", timestamp);
      return json({ error: "stale request signature" }, 401);
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

    // AUTOCOMPLETE など type 5 を許さない種別は defer しない
    return json({ error: "unsupported interaction type" }, 400);
  },
};
