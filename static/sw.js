// simple service worker for notifications and basic fetch caching (optional)
self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  self.clients.claim();
});

// handle notification click
self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const data = event.notification.data || {};
  const sender = data.sender;
  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then( clients => {
      if (clients.length > 0) {
        // focus first client and post message if needed
        clients[0].focus();
        if (sender) clients[0].postMessage({ action: 'open_chat', sender });
        return;
      }
      // if no clients, open a new one
      if (clients.openWindow) {
        return clients.openWindow('/');
      }
    })
  );
});
