# Nodeyard for iPhone

A native SwiftUI app for your nodeyard cluster. No web pages inside: it reads the dashboard's JSON API.

- **Overview**: live CPU, memory and disk rings, alerts, network, a history chart, and every node.
- **Cluster**: nodes, pods, apps, services and events, with filters and namespaces. Tap a pod for its containers
  and live logs.
- **AI**: chat with any ready model on the cluster. Answers stream in word by word.
- **Music**: your liked songs, library and playlists, plus SoundCloud search. Plays natively, keeps going with the
  screen locked, and shows on the lock screen and in Control Centre.
- **Terminal**: real terminals (SwiftTerm) on the server and the nodes.
- Sign in with the dashboard password once. It's kept in the Keychain and used to sign in again when the session
  runs out.
- Optional Face ID (or passcode) lock, which also hides the app in the app switcher.

Your phone has to reach the dashboard, so turn on Tailscale on the phone and use the tailnet name
(for example `my-mac.tailnet.ts.net`, or `http://my-mac:9092`).

## Get the IPA

GitHub Actions builds it on every push to `ios/` (workflow **iOS IPA**, or run it by hand from the Actions tab).
Download the `Nodeyard-ipa` artifact from the run and unzip it to get `Nodeyard.ipa`.

With the GitHub CLI:

```bash
gh workflow run ios-ipa.yml --ref feat/ios-app
gh run download --name Nodeyard-ipa
```

## Put it on the iPhone

The IPA isn't signed. Sign and install it with one of these:

- **Sideloadly** (Mac or Windows): plug the phone in, drop `Nodeyard.ipa` in, sign in with your Apple ID, Start.
- **AltStore**: open AltStore on the phone, My Apps, +, pick `Nodeyard.ipa`.

Then on the phone: Settings, General, VPN & Device Management, trust your Apple ID.
On iOS 16 and later also turn on Settings, Privacy & Security, Developer Mode.

With a free Apple ID the app stops opening after 7 days. Refresh it in AltStore (it can do this by itself on
your Wi-Fi) or install it again with Sideloadly. A paid developer account lasts a year.

## Build it yourself

You need Xcode and XcodeGen (`brew install xcodegen`):

```bash
cd ios/NodeyardMobile && xcodegen generate && open NodeyardMobile.xcodeproj
```
