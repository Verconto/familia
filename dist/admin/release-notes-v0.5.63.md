## What's new in v0.5.63

- Backend image now downloaded from GitHub during install/update (SSH fallback)
- Install/update survives SSH drops — runs detached on the VM, admin re-attaches
- Forwarded and replied-to messages re-processed: voice, photos, files, video
- Configurable STT audio budget per inbound message (Channels → STT card)
- Admin version shown in window title
- Bug fixes

---

Backend: `0.2.8`
SHA256 `FamiliaAdmin-v0.5.63.exe`: `f2c11f44446b21de46154f53cfc29c7ba05e060bc9897012569863a87050b9e6`
SHA256 `familia-source-v0.5.63.tar.gz`: `89fc30b42a48554f8026571ae4723208d72cf143c6c456008f9f5b9736cff81e`
Built from commit: `b380e69`

> **Note (2026-05-04):** This release was rebuilt to bundle the latest backend 0.2.8 (the original v0.5.63 from 2026-05-03 erroneously shipped backend 0.2.4 with release-notes claiming 0.2.5). Re-download if you installed before this date.
