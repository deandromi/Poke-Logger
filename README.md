# Poké-Logger scanner database

This repository builds and hosts the Android scanner's **real compact card-reference index**.

## How it works

- GitHub Actions downloads TCGdex English card metadata and small reference images **once on the runners**, not on each phone.
- Images are converted to the Android v2 format: two 64-bit difference hashes and 27 RGB samples per card.
- Eight parallel jobs build shards, then validate and publish `scanner-index-v2.zip` to GitHub Releases.
- Publication is blocked below **95% indexed coverage**. This is a coverage threshold, **not 95% recognition accuracy**.
- The ZIP includes `manifest.txt`, `scanner-card-catalogue-v2.tsv`, and `scanner-image-signatures-v2.tsv`. The Android app checks per-file SHA-256 hashes before importing.
- Full-resolution card artwork is not redistributed. Card references point to [TCGdex](https://tcgdex.dev/).

## Build the first release

Open **Actions → Build Android scanner index → Run workflow**. Generating reference fingerprints for the first time can take a while and may encounter upstream rate limits. The workflow will not release incomplete data.

After a successful run the stable download URL is:

```
https://github.com/deandromi/Poke-Logger/releases/latest/download/scanner-index-v2.zip
```

Put that URL into the Android project's `android/src/main/assets/scanner-index-source.txt` and rebuild the app. **The URL returns 404 until a real release has been successfully published.** Existing on-device fingerprints are preserved.

This index is a speed/distribution improvement; identification quality still requires real-card tests (holo glare, tilt, multiple printings). The Python Pillow resampling may not be pixel-identical to Android Bitmap scaling.
