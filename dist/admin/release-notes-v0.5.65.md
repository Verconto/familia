## What's new in v0.5.65

- Connect screen now shows a "GitHub release" badge — green when up to
  date, amber with a Download button when a newer version is out, red
  with the actual error text when GitHub can't be reached. No more
  silent failures of the auto-update probe.
- Fixes the CSP that was blocking the auto-update check from reaching
  GitHub. Previous releases never showed the "new version available"
  dialog because the webview refused the network request.
- Backend unchanged (still `0.2.9`).
- Bug fixes.

---

> One-time manual upgrade required: if you're on v0.5.64 or earlier,
> the "new version available" dialog never fired for you because of
> the CSP issue this release fixes. Download v0.5.65 by hand from the
> releases page; from v0.5.65 onward, future updates surface
> automatically on the Connect screen.

---

SHA256 `FamiliaAdmin-v0.5.65.exe`: `2f3f9bdcf592057055358bbeb9e29abda6a6b6716686f5fa1ecad6b058053e8a`
SHA256 `familia-source-v0.5.65.tar.gz`: `353db20e2e3333ec5fc325cf19caba95e293a54bdd14377b52b71baa1f6cdcce`
