ILU AI STUDIO - Website starter project

Included files:
- index.html: responsive multi-section website and functional local photo editor
- manifest.webmanifest: PWA manifest
- sw.js: basic offline app-shell cache
- icon.svg: app icon

Implemented:
- Responsive home, editor, enhancement, background, upscaler, projects, settings, about sections
- Local image selection and camera input
- Brightness, contrast, saturation, exposure, warmth and blur
- Rotate, flip, grayscale, center-square crop
- Before/after preview
- Undo/redo for slider and transform changes
- PNG export
- IndexedDB local project saving/deleting
- Dark/light theme
- Basic PWA files

Not implemented:
- Real AI enhancement/upscaling, segmentation/background removal, object removal
- Authentication, cloud sync, hosted public URL
- This project has not been deployed or independently tested on every device/browser.

To publish:
Upload all files to a static HTTPS host or import into a suitable hosting workflow. PWA installation requires HTTPS (or localhost).
iPhone: open published URL in Safari > Share > Add to Home Screen.
Android: browser menu > Install app/Add to Home screen if offered.

Privacy: image editing runs in the browser; this starter does not upload photos to a server.
