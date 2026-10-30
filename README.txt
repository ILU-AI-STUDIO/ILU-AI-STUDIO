ILU AI STUDIO — FREE UPGRADE (BETA)

WHAT IS INCLUDED
- Existing local photo editor and local project storage.
- Smart Enhance: local auto-levels/contrast processing (NOT generative AI).
- 2x/4x image resizing (standard interpolation, NOT AI super-resolution).
- AI Background Remover: uses the open-source @imgly/background-removal browser library. The runtime/model is downloaded from a public CDN when first used. Internet is required for first use; mobile memory limits may cause failure on large photos.

NOT INCLUDED YET
- AI face enhancement, AI restoration, object removal, true AI upscaling, login or cloud sync.
- No API key or paid API is configured.

HOW TO UPDATE YOUR GITHUB PAGES SITE FROM IPHONE
1. Download and extract this ZIP using the iPhone Files app.
2. Open your GitHub repository: ilu-ai-studio (the repo name is case-sensitive only in the URL path).
3. In the repository, open Add file > Upload files.
4. Upload the CONTENTS of the ILU_AI_STUDIO folder: index.html, manifest.webmanifest, icon.svg, sw.js, README.txt. Do not upload the ZIP itself or the outer folder.
5. If GitHub asks, choose to replace/overwrite the existing files, then Commit changes to the main branch.
6. Wait 2–5 minutes. Open your published site and refresh. If the old version remains, close the tab and reopen; on iPhone Safari, clear website data for the site only if needed.

IMPORTANT
- The first AI background removal may take time because model files are downloaded. Keep the page open and use a stable internet connection.
- This is a free community model integration, not a guaranteed hosted service. CDN/model availability, licence terms, device/browser support and free-tier conditions can change.
- The model is not bundled in this ZIP. The image selected for removal is processed in the browser by the library; the library fetches its model assets from its configured public CDN.
- Do not put private API keys in index.html.
