// Minimal service worker — required for PWA installability on Android Chrome.
// We deliberately do NOT cache app assets: the app is served with no-store
// headers and evolves frequently. This worker just claims clients and passes
// fetches through to the network.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // Pass-through; let the browser handle it normally.
});
