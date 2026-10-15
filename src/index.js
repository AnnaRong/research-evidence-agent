import { DurableObject, scheduler, snapshotContainer } from "cloudflare:workers";

const SNAPSHOT_KEY = "latest-filesystem-snapshot";
const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const PORT = 8080;

function containerEnv(env) {
  return {
    HOST: "0.0.0.0",
    PORT: String(PORT),
    REA_STATE_DB: "/data/research_agent.sqlite3",
    REA_FEEDBACK_DIR: "/data/feedback",
    REA_ANALYTICS_DIR: "/data/analytics",
    EDISON_MCP_COMMAND: "edison-mcp",
    EDISON_MCP_ARGS: "stdio",
    OPENAI_API_KEY: String(env.OPENAI_API_KEY || ""),
    OPENAI_MODEL: String(env.OPENAI_MODEL || "gpt-5-mini"),
    OPENAI_BASE_URL: String(env.OPENAI_BASE_URL || ""),
    ANTHROPIC_API_KEY: String(env.ANTHROPIC_API_KEY || ""),
    ANTHROPIC_AUTH_TOKEN: String(env.ANTHROPIC_AUTH_TOKEN || ""),
    ANTHROPIC_MODEL: String(env.ANTHROPIC_MODEL || "claude-sonnet-4-5"),
    ANTHROPIC_BASE_URL: String(env.ANTHROPIC_BASE_URL || ""),
    ELICIT_MCP_TOKEN: String(env.ELICIT_MCP_TOKEN || ""),
    ELICIT_API_KEY: String(env.ELICIT_API_KEY || ""),
    ELICIT_MCP_URL: String(env.ELICIT_MCP_URL || "https://elicit.com/api/mcp"),
    EDISON_API_KEY: String(env.EDISON_API_KEY || ""),
    REA_MODEL_REQUESTS_PER_HOUR: String(env.REA_MODEL_REQUESTS_PER_HOUR || "20")
  };
}

export class ResearchAgentContainer extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env;
    this.container = ctx.container;
    this.starting = null;
  }

  async waitUntilReady() {
    const port = this.container.getTcpPort(PORT);
    for (let attempt = 0; attempt < 90; attempt += 1) {
      try {
        const response = await port.fetch("http://container/api/health");
        if (response.status < 500) return;
      } catch (_) {
        // The Python process is still starting.
      }
      await scheduler.wait(1000);
    }
    throw new Error("Research Agent container did not become ready within 90 seconds");
  }

  async startContainer() {
    const savedSnapshot = await this.ctx.storage.get(SNAPSHOT_KEY);
    const common = {
      instance: "basic",
      enableInternet: true,
      env: containerEnv(this.env)
    };

    this.container.setInactivityTimeout(10 * 60 * 1000);
    this.container.start(savedSnapshot
      ? { ...common, containerSnapshot: savedSnapshot }
      : { ...common, image: this.container.images.default });

    try {
      await this.waitUntilReady();
    } catch (error) {
      if (!savedSnapshot) throw error;
      await this.container.destroy();
      await this.ctx.storage.delete(SNAPSHOT_KEY);
      this.container.start({ ...common, image: this.container.images.default });
      await this.waitUntilReady();
    }
  }

  async ensureStarted() {
    if (this.container.running) return;
    if (!this.starting) {
      this.starting = this.startContainer().finally(() => {
        this.starting = null;
      });
    }
    await this.starting;
  }

  async saveSnapshot() {
    const snapshot = await snapshotContainer(this.container, true);
    await this.ctx.storage.put(SNAPSHOT_KEY, snapshot);
  }

  async fetch(request) {
    await this.ensureStarted();
    this.container.setInactivityTimeout(10 * 60 * 1000);

    const target = new URL(request.url);
    target.protocol = "http:";
    target.hostname = "container";
    target.port = "";
    const response = await this.container.getTcpPort(PORT).fetch(new Request(target, request));
    if (!WRITE_METHODS.has(request.method)) return response;

    const body = await response.arrayBuffer();
    const headers = new Headers(response.headers);
    try {
      await this.saveSnapshot();
      headers.set("X-Research-Agent-Snapshot", "saved");
    } catch (error) {
      console.error("Failed to save container snapshot", error);
      headers.set("X-Research-Agent-Snapshot", "failed");
    }
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers
    });
  }
}

export default {
  async fetch(request, env) {
    const id = env.RESEARCH_AGENT.idFromName("production");
    return env.RESEARCH_AGENT.get(id).fetch(request);
  }
};
