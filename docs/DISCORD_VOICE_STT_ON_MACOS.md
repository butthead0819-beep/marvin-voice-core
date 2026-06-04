# Getting Discord voice audio into STT — the DAVE wall (and macOS)

> Field notes from building [Marvin](../README.md), a Discord voice companion. If you're
> writing a bot that **listens** to a Discord voice channel and runs speech-to-text, you
> will hit the wall described here. This page is the thing I wish I'd found when I was stuck.
> It cost me weeks; it should cost you an afternoon.

If you only read one thing: **a bot that "joins voice but transcribes nothing" is almost
always a decryption problem, not an audio problem.** Discord turned on end-to-end
encryption (DAVE), your receive library only undoes the *outer* layer, and the *inner*
ciphertext gets fed to your STT as garbage. The bot is alive but deaf.

---

## TL;DR — the version combo that actually works

Most of the pain is dependency hell. This is the set that works together (Python, late 2026):

```
discord.py == 2.7.1            # MUST be 2.7.x — it ships the DAVE handshake (opcodes 21–31 + MLS state)
discord-ext-voice_recv == 0.5.2a179   # receives audio + undoes outer SRTP — but has NO DAVE support
davey == 0.1.5                 # Snazzah/davey (a DAVE library from GitHub) — you call it for the inner decrypt
```

The trick: **discord.py already does the entire DAVE key exchange for you** and keeps a
live `voice_client._connection.dave_session`. `voice_recv` does **not** know DAVE exists.
So you don't reimplement MLS — you just bolt the *final decrypt step* onto the receive path.

---

## The wall: two layers of encryption, your library only undoes one

After Discord enables DAVE on a guild, every inbound audio packet is encrypted **twice**:

```
Discord UDP packet
  └─ outer: SRTP            (aead_xchacha20_poly1305_rtpsize)   ← voice_recv handles this
       └─ inner: DAVE E2EE  (MLS group encryption)              ← nobody handles this for you

What you want:
UDP packet → decrypt SRTP → decrypt DAVE → Opus → PCM → VAD → STT
What you get out of the box:
UDP packet → decrypt SRTP → [still ciphertext] → Opus(garbage) → STT(0 results)
```

`voice_recv` happily strips the SRTP layer and passes the DAVE ciphertext straight through.
Your STT receives noise and returns nothing. Logs look "fine." The bot is connected,
speaking works (TTS is outbound, unaffected) — it just can't hear anyone.

### How to know this is your problem (diagnosis before you change code)

You're hitting the DAVE wall if you see any of these:

- **`nacl.exceptions.CryptoError`** spamming your logs, alongside **zero** real audio frames.
- **`Received packet for unknown ssrc <N>, size=12`** repeating, with no audio-sized
  packets (≥ ~50 bytes) ever arriving.
- Your transcription count **drops to 0 on a specific date** and never recovers — for us it
  was the day Discord flipped DAVE on for the guild (the voice gateway `op 4` payload starts
  carrying `dave_protocol_version: 1` / `secure_frames_version: 1`).
- The bot is otherwise healthy: it joins, it talks, presence is fine. **Alive but deaf.**

Do NOT assume "I'll just upgrade and it'll fix itself." `voice_recv` has no DAVE PR. An
upgrade will not save you. You have to patch the decrypt step yourself.

---

## The fix: patch only the final decrypt, in one place

The whole solution is small because discord.py carries the hard part. This is the actual
inner-decrypt step (lightly trimmed from Marvin's `discord_voice_engine.py`):

```python
import davey

def maybe_dave_decrypt(voice_client, packet, plaintext: bytes) -> bytes:
    state = voice_client._connection                 # discord.py keeps the live DAVE session here
    if state is None or not getattr(state, "dave_ready", False):
        return plaintext                             # handshake not done yet → passthrough (plaintext)
    uid = voice_client._ssrc_to_id.get(packet.ssrc)  # map the RTP ssrc to a Discord user id
    if uid is None:
        return plaintext
    try:
        return state.dave_session.decrypt(uid, davey.MediaType.audio, plaintext)
    except Exception:
        return plaintext                             # passthrough frames aren't DAVE-encrypted
```

You wrap `voice_recv`'s existing `decryptor.decrypt_rtp` (which does the outer SRTP) so its
output flows through `maybe_dave_decrypt` before reaching Opus/STT. Same place is where you
handle key resync (see landmine #2): on a `CryptoError`, re-read `voice_client.secret_key`,
call `decryptor.update_secret_key(...)`, and retry once.

Principles that saved me re-debugging this for weeks:

1. **Gate on `dave_ready`.** Decrypting before the MLS key exchange finishes crashes. Until
   ready, pass packets through untouched (early/passthrough frames are plaintext anyway).
2. **Keep ALL decryption in one function.** Don't scatter DAVE logic across your sink, your
   reader, and your cog. In Marvin it lives in one patch (`discord_voice_engine.py`,
   `patch_voice_recv_key_sync()`), called from every "join voice" path. One place to reason
   about, one place to fix.
3. **`davey` exception → return the SRTP plaintext, don't drop the packet.** Passthrough
   mode frames are not DAVE-encrypted; treating a decrypt failure as "use what you have" is
   correct, not a hack.
4. **You do NOT touch opcodes 21–31.** discord.py wires the entire DAVE handshake and MLS
   group state. You only consume `dave_session.decrypt(...)`. Resist the urge to "handle the
   protocol" — it's already handled.
5. **Test it live.** A mock voice client never runs a real SRTP/MLS handshake, so unit tests
   will pass while the real path is broken. The only ground truth is a real voice channel.

Ground truth that it's working: a continuous stream of successful transcriptions in your
logs (for us: `✅ [STT Output] <speaker>: <text>` and `🚀 [Sink] first valid voice (DAVE+)`).
Watch the *raw STT output*, not your high-level event log — STT can be running perfectly
while your "wake word fired" log stays silent.

Real implementation to crib from: [`discord_voice_engine.py`](../discord_voice_engine.py)
(`patch_voice_recv_key_sync`, the `[KeySync]` patch, `RealtimeVADSink.write`). Broader
pipeline writeup: [`docs/stt_workflow.md`](stt_workflow.md), [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Bonus landmines (each cost me real time)

### 1. macOS UDP capture
On macOS you may need a UDP-layer fix before any of the above matters. Marvin keeps it in
[`davey_bridge.py`](../davey_bridge.py). Note: the `apply_davey_fix()` "DaveSession→MLSContext
shim" in there is for **Pycord**, not discord.py — but its macOS UDP patch is the useful part.
If you're on discord.py, take the UDP fix and ignore the shim.

### 2. Key resync on reconnect (the "it broke for no reason" storm)
Symptom: STT was fine, then suddenly **transcripts go garbled** and `CryptoError` jumps from
~0/min to ~80/min. Cause: **someone changed the channel bitrate** (or anything that forces a
voice renegotiation). The reconnect hands out a new `secret_key`, and if your receive layer
doesn't cleanly pick it up, every inbound packet fails to decrypt. Fixes, in order of
robustness: re-read `voice_client.secret_key` on `CryptoError`; and a watchdog that, if it
sees packets arriving but **zero successful decrypts** past a short grace window, escalates
to a full reconnect/restart. Beware the inverted-logic trap: a naive "grace period" that
resets on every reconnect means **the flakier the connection, the less your auto-heal fires**.
Gate the grace on "have we decrypted ≥1 packet since connecting," not just wall-clock time.

### 3. Stale process running stale code
A whole class of "my fix didn't work" is just **the old process still running the old code**.
When something looks unfixed after an edit, compare process start time against file mtime
(`ps -o lstart= -p <PID>` vs the file's `mtime`) before you debug anything else. Restart,
then debug.

---

## Honest scope

- This is **macOS / Apple Silicon** territory (where Marvin lives). The decryption story is
  platform-independent, but the UDP/audio capture specifics are not.
- `voice_recv` is pre-release software with no DAVE roadmap. Expect to keep this patch across
  upgrades, or move it into a `voice_recv` subclass you control.
- Versions move. The combo above is a known-good snapshot, not a promise. If you land a newer
  working set, that knowledge is exactly the kind of thing worth writing down for the next person.

---

*Building the same thing? Open a [Discussion](https://github.com/butthead0819-beep/marvin-voice-core/discussions) —
"I got Discord voice → STT working on my machine" is the single most useful signal this project can get.*
