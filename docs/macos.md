# Nodeyard AI for macOS

Nodeyard AI is a native SwiftUI client for the Nodeyard dashboard. It saves chats on the Mac, can sync text conversations to the dashboard, streams from split or Ollama models, and uses the shared server API key.

See [macOS app README](../macos/README.md) for setup, the 50 app capabilities, storage location and packaging instructions.

The server API added for the app is:

- `GET /api/v1/targets` lists available model targets and readiness.
- `POST /api/v1/chat/completions` accepts `model`, `messages`, `temperature` and `max_tokens`, and returns Server-Sent Events.

Both endpoints use the existing shared API key gate. Chat messages can include the same inline PNG, JPEG and WebP data URLs as the dashboard. The app sends images to the model only for the request; its optional server chat sync stores message text without uploading attachment contents.
