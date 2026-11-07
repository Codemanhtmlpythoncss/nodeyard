# Nodeyard AI for macOS

Nodeyard AI is a native SwiftUI client for the Nodeyard dashboard. It saves chats on the Mac, can sync text conversations to the dashboard, streams from split or Ollama models, and uses the shared server API key.

See [macOS app README](../macos/README.md) for setup, the 50 app capabilities, storage location and packaging instructions.

The server API added for the app is:

- `GET /api/v1/models` returns an OpenAI-compatible model list (`object: "list"`, `data[]`) and retains Nodeyard inventory fields. Unloaded installed models stay visible.
- `GET /api/v1/targets` lists Nodeyard model targets and readiness.
- `POST /api/v1/chat/completions` accepts `model`, `messages`, sampling fields and function tools. It returns a JSON completion by default, or OpenAI Server-Sent Events when `stream: true`. An unloaded split model is loaded before the answer; Ollama loads an installed model when the request reaches it.

For Hermes and other OpenAI-compatible clients, set the base URL to `https://<dashboard-host>/api/v1` and use the shared server API key. Loading events may include Nodeyard's optional progress and ETA metadata; unknown progress stays unknown.

All three endpoints use the shared API key gate. Chat messages can include the same inline PNG, JPEG and WebP data URLs as the dashboard. The app sends images to the model only for the request; its optional server chat sync stores message text without uploading attachment contents.
