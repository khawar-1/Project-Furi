# Wake-word models (Phase 12.2)

On-device "Hey Jarvis" detection. These three ONNX models run locally in the
renderer via `onnxruntime-web` (see `src/lib/wakeWord.ts`) — raw audio never
leaves the machine.

| File | Role | Input → Output |
|---|---|---|
| `melspectrogram.onnx` | audio → mel spectrogram | `[1, samples]` → `[1, 1, frames, 32]` (1760 samples → 8 frames) |
| `embedding_model.onnx` | 76 mel frames → embedding | `[1, 76, 32, 1]` → `[1, 1, 1, 96]` |
| `hey_jarvis_v0.1.onnx` | 16 embeddings → wake score | `[1, 16, 96]` → `[1, 1]` |

## Provenance & license

From **openWakeWord** (https://github.com/dscripka/openWakeWord),
release **v0.5.1**, `resources/models/`. openWakeWord is **Apache-2.0**;
`melspectrogram.onnx` and `embedding_model.onnx` are Google's shared feature
models (also Apache-2.0). The `hey_jarvis` classifier is a pretrained openWakeWord
model — the wake phrase is literally "Hey Jarvis".

Re-fetch (if ever needed):

```
base=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1
curl -fSL $base/melspectrogram.onnx  -o melspectrogram.onnx
curl -fSL $base/embedding_model.onnx -o embedding_model.onnx
curl -fSL $base/hey_jarvis_v0.1.onnx -o hey_jarvis_v0.1.onnx
```
