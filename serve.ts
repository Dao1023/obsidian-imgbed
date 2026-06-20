// 临时本地服务器，用于 dashboard.html 调试
import { serve } from "bun";

const PORT = 8787;
const ROOT = import.meta.dir;

serve({
  port: PORT,
  async fetch(req) {
    const url = new URL(req.url);
    let path = decodeURIComponent(url.pathname);
    if (path === "/") path = "/dashboard.html";
    const file = Bun.file(ROOT + path);
    if (await file.exists()) {
      return new Response(file, {
        headers: { "Cache-Control": "no-store" },
      });
    }
    return new Response("404", { status: 404 });
  },
});

console.log(`🚀 dashboard: http://localhost:${PORT}/`);
