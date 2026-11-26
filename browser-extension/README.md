# StreamRec Account Connector

This small, local-only Chromium extension does not log in on your behalf. It opens the selected platform's official login page, then imports only that platform's cookies after your explicit click and a temporary site permission. It never reads your passwords, browser history, or cookies from other domains. It removes the site permission after import and does not save cookie values in extension storage. Cookie credentials are sent only to `http://127.0.0.1:8080` using a one-use 5-minute code created by a logged-in StreamRec session. StreamRec stores the scoped cookie file with mode 0600 and passes a private snapshot only to the matching platform's recorder. Import does not establish that the session is still authenticated; reconnect after expiry.

## One-time installation (Chrome / Brave / Chromium)

1. Open `chrome://extensions` (or `brave://extensions`).
2. Enable Developer mode and choose **Load unpacked**.
3. Select the `browser-extension` directory in your cloned StreamRec repository (e.g. `~/streamerREC/browser-extension`).
4. Open StreamRec → Settings → Connected Accounts → Connect account. The connector page should open automatically. Sign in on the official platform page, then click **Import this platform session**. If the connector does not open, copy the pairing code from StreamRec and paste it in the extension popup.

No browser profile, Docker socket, or additional host directories need to be mounted. You can remove the stored session from StreamRec or uninstall the extension at any time. If you use Firefox or prefer not to install an extension, manually import a Netscape `cookies.txt` file for that platform from Connected Accounts instead.
