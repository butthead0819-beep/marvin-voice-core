# Platform philosophy

> Why Marvin is a macOS / Apple Silicon product, and why that's a feature, not a limitation.

**Marvin is a Mac product.** Tuned for Apple Silicon — Swift STT + Gemini/Groq APIs runs smoothly on M1 8GB. The Whisper-only fallback exists in `stt_handler.py` as community territory, but the maintainer doesn't test it. Adding Whisper to the same machine costs the smooth experience the design depends on; that tradeoff is the product, not a limitation. PRs that improve Linux are welcome; Linux is not the roadmap.

---

## Platform commitment

**Marvin targets macOS on Apple Silicon, 8GB+, with hybrid local-and-API components.** The reasoning:

- Swift STT (free, fast, ships with macOS) gives near-perfect transcription with no GPU cost
- Adding Whisper to take Swift's place adds 700MB–3GB of model load + meaningful CPU/swap pressure on smaller Macs
- The maintainer's own M1 8GB is the reference machine — what runs smoothly there is the bar

This is a deliberate product decision, not an oversight. "Cross-platform OSS" is a tax on the user experience when one of those platforms requires substituting heavy components. Mac users get a polished thing; other-platform users can fork.

If someone contributes solid Linux support (tested, documented, won't degrade the Mac path), PRs are welcome. The Whisper-only fallback in `stt_handler.py` is the scaffolding for that future contributor — it's not vapor, but it's not maintained either.

**Docker isn't on the roadmap.** Macs can't containerize their native audio. Even running Whisper-mode in Linux containers on a Mac host trades the smooth experience for portability the maintainer doesn't need.

## Tested footprint

Reference machine: M1 MacBook Air, 8GB. Marvin's typical resident usage is 500MB–1.2GB, leaving ~6.5GB headroom for macOS + Discord + a browser tab or two. The Apple Silicon family has had an 8GB RAM floor across every chip since M1 (Nov 2020) through M3 (2023); M4 (2024) raised the floor to 16GB. The reference machine IS the floor — if it runs smoothly here, it runs smoothly on every other Apple Silicon Mac.

Approximate scale: ~100M Apple Silicon units shipped between Nov 2020 and end of 2024 (Apple quarterly reports + analyst estimates). Of those, ~80–120M are estimated to be in active use as of 2025. That's the addressable market for Marvin in one sentence.

Caveats: the "smooth" claim assumes nothing else heavy is running (no Logic Pro + 100 Chrome tabs). It also assumes the default Swift STT path — switching to the Whisper-only fallback on the same Mac changes everything (Whisper adds 700MB–3GB of model load + sustained CPU/swap pressure).
