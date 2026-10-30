const CACHE = 'ilu-ai-studio-v3';
const CORE = ['./manifest.webmanifest','./icon.svg'];
self.addEventListener('install', event => { self.skipWaiting(); event.waitUntil(caches.open(CACHE).then(c => c.addAll(CORE)).catch(()=>{})); });
self.addEventListener('activate', event => { event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('ilu-ai-studio-') && k !== CACHE).map(k => caches.delete(k)))).then(()=>self.clients.claim())); });
self.addEventListener('fetch', event => {
  const u = new URL(event.request.url);
  if (event.request.method !== 'GET' || u.origin !== self.location.origin) return;
  // Always fetch HTML fresh so GitHub Pages updates are not hidden by an old app shell.
  if (event.request.mode === 'navigate' || u.pathname.endsWith('.html') || u.pathname.endsWith('/')) {
    event.respondWith(fetch(event.request).catch(()=>caches.match(event.request))); return;
  }
  event.respondWith(caches.match(event.request).then(hit => hit || fetch(event.request).then(r => { if(r.ok){const copy=r.clone();caches.open(CACHE).then(c=>c.put(event.request,copy));} return r; })));
});
