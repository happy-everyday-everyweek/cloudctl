// cloudctl Hub —— 跑在 Cloudflare Workers 上的无服务器核心服务端
//
// 与 Python 版 server/ 兼容同一套协议：agent 拨 wss://<host>/ws/agent?device_id=..&token=..，
// 控制台拨 wss://<host>/ws/console?token=..&device=..，另有 /api/devices、/api/devices/:id/cmd、/api/audit。
//
// 架构：Worker 只做路由，所有连接与状态都放在一个 Durable Object（Hub）里；
// 用 hibernation 接受 WebSocket，空闲时不占时长，适合 Workers 免费额度。
//
// 必需变量（wrangler secret put）：
//   HUB_TOKEN  共享令牌，agent 与控制台都用它
// 可选变量：
//   AUDIT_MAX  审计条数上限，默认 500

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const group = url.searchParams.get("group") || "default";
    const id = env.HUB.idFromName(group);
    return env.HUB.get(id).fetch(request);
  },
};

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function cors(resp) {
  const h = new Headers(resp.headers);
  h.set("access-control-allow-origin", "*");
  h.set("access-control-allow-headers", "content-type, x-token");
  h.set("access-control-allow-methods", "GET, POST, OPTIONS");
  return new Response(resp.body, { status: resp.status, headers: h });
}

export class Hub {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.auditMax = Number(env.AUDIT_MAX || 500);
    this.audit = [];
    this.pending = new Map(); // 命令 id -> {resolve, timer, from}
  }

  token() {
    return this.env.HUB_TOKEN || "";
  }

  authed(url, request) {
    const t = url.searchParams.get("token") || request.headers.get("x-token") || "";
    if (!this.token()) return true; // 未设令牌则不鉴权，仅建议内网调试
    return t === this.token();
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "OPTIONS") return cors(json({ ok: true }));

    if (path === "/health") {
      return json({ ok: true, device_count: this.state.getWebSockets("agents").length,
                    console_count: this.state.getWebSockets("consoles").length,
                    hub: "cloudflare-worker" });
    }

    if (!this.authed(url, request)) {
      return json({ ok: false, err: "令牌无效" }, 401);
    }

    if (path === "/ws/agent") return this.acceptAgent(request, url);
    if (path === "/ws/console") return this.acceptConsole(request, url);
    if (path === "/api/devices") return json({ ok: true, data: await this.deviceList() });
    if (path === "/api/audit") return json({ ok: true, data: this.audit.slice(-100) });

    const m = path.match(/^\/api\/devices\/([^/]+)\/(cmd|rules)$/);
    if (m && request.method === "POST") {
      const deviceId = decodeURIComponent(m[1]);
      const body = await request.json().catch(() => ({}));
      if (m[2] === "cmd") {
        const res = await this.dispatch(deviceId, body.op || "agent.info", body.args || {},
                                       Number(body.timeout || 60) * 1000, "rest");
        return json(res);
      }
      const res = await this.sendToAgent(deviceId, { type: "rules", rules: body.rules || {},
                                                    version: Number(body.version || 0) });
      return json({ ok: res, version: Number(body.version || 0) });
    }

    return json({ ok: false, err: "未知路径" }, 404);
  }

  // ---------------------------------------------------------------- WebSocket
  acceptAgent(request, url) {
    if (request.headers.get("Upgrade") !== "websocket") {
      return json({ ok: false, err: "需要 WebSocket 升级请求" }, 426);
    }
    const deviceId = url.searchParams.get("device_id") || "unknown";
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.state.acceptWebSocket(server, [`agent:${deviceId}`, "agents"]);
    server.serializeAttachment({ role: "agent", device_id: deviceId });
    this.note(`agent 上线 ${deviceId}`);
    return new Response(null, { status: 101, webSocket: client });
  }

  acceptConsole(request, url) {
    if (request.headers.get("Upgrade") !== "websocket") {
      return json({ ok: false, err: "需要 WebSocket 升级请求" }, 426);
    }
    const watch = url.searchParams.get("device") || "";
    const tags = ["consoles"];
    if (watch) tags.push(`watch:${watch}`);
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.state.acceptWebSocket(server, tags);
    server.serializeAttachment({ role: "console", watch });
    this.note(`控制台接入${watch ? "，观察 " + watch : ""}`);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, raw) {
    let msg;
    try {
      msg = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw));
    } catch {
      return;
    }
    const meta = ws.deserializeAttachment() || {};

    if (meta.role === "agent") {
      const deviceId = meta.device_id;
      if (msg.type === "hello") {
        await this.state.storage.put(`dev:${deviceId}`, { ...msg, device_id: deviceId,
                                                         last_seen: Date.now() });
        this.note(`agent 握手 ${deviceId}`);
        return;
      }
      if (msg.type === "result" && msg.id && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id);
        clearTimeout(p.timer);
        this.pending.delete(msg.id);
        p.resolve(msg);
        return;
      }
      if (msg.type === "event") {
        this.note(`${deviceId} 事件 ${msg.event}`);
        await this.state.storage.put(`evt:${deviceId}`, { ...msg, ts: Date.now() });
      }
      // 事件与桌面帧都转给正在看这个设备的控制台
      this.broadcast(`watch:${deviceId}`, { ...msg, device_id: deviceId });
      return;
    }

    if (meta.role === "console") {
      if (msg.type === "watch") {
        const tags = ["consoles"];
        if (msg.device) tags.push(`watch:${msg.device}`);
        try {
          this.state.acceptWebSocket(ws, tags);
        } catch {
          // 已 accepted 的情况下忽略
        }
        return;
      }
      if (msg.type === "cmd") {
        const res = await this.dispatch(msg.device_id || "", msg.op || "agent.info", msg.args || {},
                                       Number(msg.timeout || 60) * 1000, "ws");
        try {
          ws.send(JSON.stringify({ type: "result", op: msg.op, ...res }));
        } catch {}
        return;
      }
      if (msg.type === "rules") {
        await this.sendToAgent(msg.device_id || "", { type: "rules", rules: msg.rules || {},
                                                       version: Number(msg.version || 0) });
      }
    }
  }

  async webSocketClose(ws) {
    const meta = ws.deserializeAttachment() || {};
    if (meta.role === "agent") this.note(`agent 断开 ${meta.device_id}`);
  }

  // ---------------------------------------------------------------- 转发
  agentSocket(deviceId) {
    const list = this.state.getWebSockets(`agent:${deviceId}`);
    return list && list.length ? list[0] : null;
  }

  sendToAgent(deviceId, obj) {
    const ws = this.agentSocket(deviceId);
    if (!ws) return false;
    try {
      ws.send(JSON.stringify(obj));
      return true;
    } catch {
      return false;
    }
  }

  broadcast(tag, obj) {
    const payload = JSON.stringify(obj);
    for (const ws of this.state.getWebSockets(tag)) {
      try {
        ws.send(payload);
      } catch {}
    }
  }

  dispatch(deviceId, op, args, timeoutMs, from) {
    const id = crypto.randomUUID().replace(/-/g, "").slice(0, 16);
    this.note(`${from} 下发 ${op} -> ${deviceId}`);
    const sent = this.sendToAgent(deviceId, { type: "cmd", id, op, args });
    if (!sent) {
      return Promise.resolve({ ok: false, err: `设备 ${deviceId} 不在线` });
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({ ok: false, err: "设备响应超时", id });
      }, Math.min(Math.max(timeoutMs, 5000), 900000));
      this.pending.set(id, { resolve, timer, from });
    });
  }

  // ---------------------------------------------------------------- 状态
  async deviceList() {
    const out = [];
    const seen = new Set();
    for (const ws of this.state.getWebSockets("agents")) {
      const meta = ws.deserializeAttachment() || {};
      if (!meta.device_id) continue;
      seen.add(meta.device_id);
      const info = (await this.state.storage.get(`dev:${meta.device_id}`)) || {};
      out.push({ device_id: meta.device_id, online: true, ...info });
    }
    const stored = await this.state.storage.list({ prefix: "dev:" });
    for (const [key, info] of stored) {
      const deviceId = key.slice(4);
      if (seen.has(deviceId)) continue;
      out.push({ device_id: deviceId, online: false, ...info });
    }
    return out;
  }

  note(text) {
    this.audit.push({ ts: Date.now(), text });
    if (this.audit.length > this.auditMax) this.audit = this.audit.slice(-this.auditMax);
  }
}
