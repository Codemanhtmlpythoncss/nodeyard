# Nodeyard AI for macOS

Nodeyard AI is a native SwiftUI chat client for the Nodeyard dashboard. It uses the server-wide API key, streams replies through the cluster, and keeps chats on the Mac with optional server sync.

## Install or update

Requires macOS 13 or later and Swift 6 (the Xcode Command Line Tools are enough). From the repository root:

```sh
sh scripts/install-macos-ai-app.sh
```

It builds the app in a private temporary folder, checks the
bundle (identifier, executable, icon, signature), then replaces `/Applications/Nodeyard AI.app`; the old copy is put back
if anything fails. Chats, settings and Keychain items are not touched. `--relaunch` also quits a running copy and opens the
new one. Settings › About shows the installed version, build number and source commit (also in the bundle's
`Info.plist` as `NodeyardSourceCommit`).

The app is signed ad hoc, so after an update macOS may ask once more to use the Keychain item and for Accessibility
access (computer use). Set `NODEYARD_AI_SIGN_IDENTITY` to a certificate you own to keep those permissions across updates.

## AI tools

Per chat (Chat settings › AI tools) or as defaults (Settings › AI tools):

- **Web search and page reading** through the Nodeyard server's internet connection.
- **Agent Browser** (⇧⌘B): a private WebKit window the AI drives with `page_open`, `page_read` (text plus numbered
  links, buttons and fields), `page_click`, `page_type`, `page_back`, `page_console` (JavaScript errors) and
  `page_snapshot`. It has its own non-persistent website data, so it never sees your Safari or Chrome sign-ins, and it
  forgets everything on quit. Every click and text entry asks you first; password fields and non-web addresses are refused.
- **Use this Mac** (Accessibility): read the frontmost app's visible text; click, type, press safe keys and open allowed
  apps, each with your approval.

## Manage the cluster (website parity)

The toolbar switches between **Chat** (⌘1), a quick **Models** list (⌘2) and **Manage** (⌘3). Manage mirrors the website:
Overview, Nodes (with Restart Kubernetes and Reboot every machine), Pods (with logs), Workloads, Network, Storage,
Hardware, AI models (split model controls, automatic unloading, downloaded models with Run and Delete, Ollama load,
unload and delete, free up space), **Research** (the server's Research Mode, with sources and the citation check),
**Plugins** (what each tool area does and whether it works), Alerts, Events, Doctor (checks and fixes), Tasks (live
output, cancel) and the in-app **Setup guide**. Nodes also shows each machine's Wi-Fi/LAN and Tailscale connection, with
Test, manual addresses and reset. Every action uses the dashboard's own API and allow-list, and destructive ones ask first.

Management needs the dashboard password (the website's sign-in), stored in Keychain only if you tick Remember; the
session cookie stays in memory. Chat, model status and automatic unloading work with the server API key alone. The
website's Terminal, Commands and Settings pages are not in the app; use the website for those.

## Build only

```sh
./scripts/build-macos-ai-app.sh
open "dist/Nodeyard AI.app"
```

You can choose another bundle path by passing it to the script:

```sh
./scripts/build-macos-ai-app.sh "$HOME/Applications/Nodeyard AI.app"
```

The bundle is signed ad hoc, so it is intended for local use or internal sharing. A full Xcode installation is not required; Swift and the macOS SDK from Command Line Tools are enough.

The build generates a branded multi-resolution `AppIcon.icns` and uses the same gradient node-network mark in the sidebar.

## Connect

Open Settings, enter the dashboard address (for example `https://nodeyard.example:9092`) and the shared server API key from Dashboard → Settings. The key is stored in macOS Keychain. The app fetches model targets and downloads from the protected `/api/v1` API, and sends chat through `/api/v1/chat/completions`. Keep TLS enabled when connecting over an untrusted network. Local chats work before a server is configured.

Enter the **dashboard** address, not the model's OpenAI-compatible URL. For a direct Nodeyard dashboard connection, use `http://<server>:9092` on a private LAN or Tailscale network; port `31435` is the model endpoint. A saved port-31435 address is corrected to the dashboard address when the app starts, and Settings offers the same one-click switch if you enter it again. For public access, use a valid HTTPS address whose certificate matches its host name.

Chat JSON is stored under `~/Library/Application Support/NodeyardAI/Chats`; uploaded files are copied to its `Attachments` folder. They stay on this Mac; remote sync stores message text and does not upload attachment contents.

## Included features

The app includes the following 50 capabilities beyond basic saved chats:

1. Multi-chat sidebar
2. Restore the most recently used chat at launch
3. Search chat titles
4. Search saved message text
5. Pin frequently used chats
6. Archive old chats
7. Restore archived chats
8. Rename chats
9. Duplicate chats
10. Undo a recent deletion
11. Export conversations as Markdown
12. Export conversations as JSON
13. Import a chat JSON file
14. Sync completed conversations to the dashboard
15. Browse server-saved chats
16. Import server chats to this Mac
17. Delete server-saved chats
18. Configure the dashboard URL
19. Authenticate with the shared server API key
20. Store the key in Keychain
21. Test server connectivity
22. Persistent connection status
23. Select a ready model from the toolbar
24. Show model readiness before chatting
25. Browse downloaded cluster models
26. Start a downloaded model from the app
27. Show in-progress model downloads
28. Per-chat context length
29. Per-chat creativity control
30. Per-chat reply-length control
31. Unlimited replies
32. Per-chat system prompt
33. Persistent defaults for new chats
34. Default reply length
35. Default context length
36. Live streamed responses
37. Stop a response immediately
38. Regenerate the last response
39. Edit and resend a user message
40. Expandable model reasoning
41. Show response duration
42. Show model tokens per second
43. Copy assistant responses
44. Render Markdown in answers
45. Select and copy any message text
46. Attach multiple images and documents
47. Attach UTF-8 plain-text, Markdown, CSV, JSON, and source-code files
48. Paste images from the clipboard
49. Drag files into a chat
50. Keep attachment files in the local app data folder with their saved chats

The dashboard server API's `/api/v1/targets` and streaming `/api/v1/chat/completions` endpoints are protected by the existing shared-key gate.
