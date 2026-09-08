/* Online-only Desk worker. No business data, API responses, or HR caches are stored. */
const PROCESS_PWA_OFFLINE_PAGE = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#2563eb"><title>请连接网络 · 恒算 ERP</title>
<style>body{font:16px/1.7 system-ui,sans-serif;margin:0;padding:48px 24px;color:#1f2937;background:#f8fafc}
main{max-width:420px;margin:12vh auto}h1{font-size:24px}p{color:#4b5563}
button{font:inherit;background:#2563eb;color:white;border:0;border-radius:8px;padding:12px 24px;min-height:48px;cursor:pointer}</style>
</head><body><main><p>恒算 ERP · 重庆恒算科技有限公司</p><h1>请连接网络后重试</h1>
<p>报工、审批和查看最新任务需要联网。网络恢复后，请刷新页面并核对操作结果。</p>
<button onclick="location.reload()">重新连接</button></main></body></html>`;

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
	const request = event.request;
	const url = new URL(request.url);
	if (
		request.method !== "GET" ||
		request.mode !== "navigate" ||
		url.origin !== self.location.origin ||
		!(url.pathname === "/desk" || url.pathname.startsWith("/desk/"))
	) return;

	event.respondWith(
		fetch(request).catch(() => new Response(PROCESS_PWA_OFFLINE_PAGE, {
			status: 503,
			headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
		}))
	);
});
