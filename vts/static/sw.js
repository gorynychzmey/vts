// vts service worker — keeps the app installable, handles Web Push and the
// POST share target for files. No asset caching; index is served no-store.

const SHARE_CACHE = "vts-share-inbox";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (err) {
      payload = { title: "vts", body: event.data.text() };
    }
  }
  const status = payload.status || "";
  const title = payload.title || "vts";
  let heading;
  if (status === "completed") heading = "vts — task complete";
  else if (status === "failed") heading = "vts — task failed";
  else heading = "vts";

  const body = status === "failed" && payload.error
    ? `${title}\n${payload.error}`.slice(0, 400)
    : title;

  const options = {
    body,
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    tag: payload.task_id || undefined,
    data: { task_id: payload.task_id || null, status },
  };
  event.waitUntil(self.registration.showNotification(heading, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const taskId = event.notification.data && event.notification.data.task_id;
  const target = taskId ? `/?task=${encodeURIComponent(taskId)}` : "/";
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of all) {
      // Focus any existing vts window and hand it the task id via postMessage.
      if (new URL(client.url).origin === self.location.origin) {
        client.postMessage({ type: "notification_click", task_id: taskId });
        return client.focus();
      }
    }
    return self.clients.openWindow(target);
  })());
});

// POST share target: Chrome on Android sends the shared payload as multipart
// to /share. We intercept, stash the file in a named cache, and redirect the
// client to the app with a marker. The page then reads the stashed file.
self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method === "POST" && url.pathname === "/share") {
    event.respondWith(handleShareTargetPost(event));
    return;
  }
  if (req.method === "GET" && url.pathname === "/_share_inbox") {
    event.respondWith(serveShareInbox());
    return;
  }
});

async function handleShareTargetPost(event) {
  try {
    const formData = await event.request.formData();
    const file = formData.get("file");
    const url = formData.get("url") || "";
    const text = formData.get("text") || "";
    const title = formData.get("title") || "";

    if (file && typeof file === "object" && file.size > 0) {
      const cache = await caches.open(SHARE_CACHE);
      const headers = new Headers({
        "Content-Type": file.type || "application/octet-stream",
        "X-Share-Filename": encodeURIComponent(file.name || "shared"),
      });
      await cache.put("/_share_inbox", new Response(file, { headers }));
      return Response.redirect("/?share_pending=file", 303);
    }

    const params = new URLSearchParams();
    if (url) params.set("share_url", url);
    if (text) params.set("share_text", text);
    if (title) params.set("share_title", title);
    const qs = params.toString();
    return Response.redirect(qs ? `/?${qs}` : "/", 303);
  } catch (err) {
    return Response.redirect("/?share_error=1", 303);
  }
}

async function serveShareInbox() {
  const cache = await caches.open(SHARE_CACHE);
  const hit = await cache.match("/_share_inbox");
  if (!hit) return new Response("", { status: 404 });
  // Serve once, then drop — keeps the cache from holding the payload forever.
  await cache.delete("/_share_inbox");
  return hit;
}
