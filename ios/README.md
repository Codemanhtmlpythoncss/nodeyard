# Nodeyard for iPhone

A native SwiftUI app around your nodeyard dashboard:

- Asks once for your dashboard address and checks it answers.
- Native tab bar: Overview, AI, Music, Terminal, and More (every other page).
- Music keeps playing with the screen locked, with lock-screen and Control Centre controls.
- Optional Face ID (or passcode) lock, which also hides the app in the app switcher.
- Pull down to reload. Links to other sites open in Safari.

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
