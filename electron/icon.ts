/**
 * Jarvis OS — App Icon (Phase 4, Part 3)
 * One 32x32 PNG (cyan HUD ring + dot, matching the frontend accents) embedded
 * as a base64 data URL. Embedded rather than loaded from disk because the
 * electron build step is plain `tsc` — it compiles .ts files only and would
 * never copy an asset file into dist-electron. Used by the tray, native
 * notifications, and the window.
 */
import { nativeImage, NativeImage } from 'electron';

const ICON_DATA_URL =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAABEklEQVR42u1Xuw2EMAxlAoa4DSJmoEzPDNkhFQswATNQsQMtt8bpyrR3juTKSpwQQLFOF+lJKIr9bPyJ0zT/dXA9nu8W0AMMwCIM7rV3Eg+AFfBJwJ8ZriT2nm0ZxBRepj9LbguIKWwp+cQo3QEzYETMuBc7P13luSdSjJzCM+V/AmNOhV8AfcABjTJUT58jvAXIVUEIVcCILafUqNX6RBLrgL6BE6B1Pl9QxjQnVq7DUWtV5GwHWAAO4b87JhRUb5uTfDtD7gJKHWPEnkxG7OfJ34/exup9yQyDyan9MaLMMQa4iMyY7AkSDKgegupJWLcMqzciEa24+mUk4jquPpCIGMlEDKUixnIRDxMxT7OfXV8flWu9i9yolQAAAABJRU5ErkJggg==';

let cached: NativeImage | null = null;

/** The Jarvis icon as a NativeImage (decoded once, then reused). */
export function appIcon(): NativeImage {
  if (cached === null) {
    cached = nativeImage.createFromDataURL(ICON_DATA_URL);
  }
  return cached;
}
