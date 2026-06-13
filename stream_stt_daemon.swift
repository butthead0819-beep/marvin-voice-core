// 常駐串流 STT daemon（Volatile Phase 1，2026-06-13 hot sprint）
// 暖模型常駐 + 每語句新 analyzer（冷載入 4.5s 只付一次；暖 prepare ~100ms）。
//
// stdin line protocol（每行一指令）：
//   R                  → reset：開新語句
//   A <base64-pcm>     → append：16kHz mono int16 LE 音訊塊（Sink 已降頻的格式）
//   F                  → finalize：收尾，吐 final
// stdout JSONL：
//   {"v":"馬文播放","t_ms":123}      volatile 假設（t_ms = 語句起算毫秒）
//   {"final":"馬文播放晴天","t_ms":456}  finalize 後
//   {"ready":true}                    模型暖好（啟動時一次）
//
// 建置：swiftc -parse-as-library -O stream_stt_daemon.swift -o stream_stt_daemon_bin
import Foundation
import Speech
import AVFAudio

enum Command { case reset, audio(Data), finalize }

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj),
       let s = String(data: d, encoding: .utf8) {
        print(s); fflush(stdout)
    }
}

@main
struct StreamDaemon {
    static func main() async {
        let localeId = ProcessInfo.processInfo.environment["STT_LOCALE"] ?? "zh-TW"
        let supported = await SpeechTranscriber.supportedLocales
        guard let locale = supported.first(where: { $0.identifier(.bcp47) == localeId })
                ?? supported.first(where: { $0.identifier(.bcp47).hasPrefix("zh") }) else {
            FileHandle.standardError.write("no zh locale\n".data(using: .utf8)!); exit(1)
        }

        // 來源格式固定：Sink 餵 16kHz mono int16（pcm48k_stereo_to_16k_mono 的輸出）
        guard let srcFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                            sampleRate: 16000, channels: 1, interleaved: true) else {
            exit(1)
        }

        // stdin 讀取在背景 thread（readLine 同步阻塞），push 進 AsyncStream
        let (commands, cmdCont) = AsyncStream.makeStream(of: Command.self)
        let reader = Thread {
            while let line = readLine(strippingNewline: true) {
                if line == "R" { cmdCont.yield(.reset) }
                else if line == "F" { cmdCont.yield(.finalize) }
                else if line.hasPrefix("A ") {
                    if let data = Data(base64Encoded: String(line.dropFirst(2))) {
                        cmdCont.yield(.audio(data))
                    }
                }
            }
            cmdCont.finish()
        }
        reader.stackSize = 1 << 20
        reader.start()

        // 暖一個 transcriber/analyzer 把模型載進記憶體
        func makeTranscriber() -> SpeechTranscriber {
            SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
        }
        var warm = makeTranscriber()
        let warmAnalyzer = SpeechAnalyzer(modules: [warm],
                                          options: .init(priority: .userInitiated, modelRetention: .processLifetime))
        if let tf = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [warm]) {
            try? await warmAnalyzer.prepareToAnalyze(in: tf)
        }
        let targetFormat = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [warm]) ?? srcFormat
        let converter = AVAudioConverter(from: srcFormat, to: targetFormat)
        emit(["ready": true])

        // 每語句狀態
        var transcriber: SpeechTranscriber? = nil
        var analyzer: SpeechAnalyzer? = nil
        var inputCont: AsyncStream<AnalyzerInput>.Continuation? = nil
        var collector: Task<Void, Never>? = nil
        var uttStart = Date()

        func pcmBuffer(_ data: Data) -> AVAudioPCMBuffer? {
            let frames = AVAudioFrameCount(data.count / 2)
            guard frames > 0,
                  let inBuf = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: frames) else { return nil }
            inBuf.frameLength = frames
            _ = data.withUnsafeBytes { raw in
                memcpy(inBuf.int16ChannelData![0], raw.baseAddress!, data.count)
            }
            guard let conv = converter else { return inBuf }
            let ratio = targetFormat.sampleRate / srcFormat.sampleRate
            let cap = AVAudioFrameCount(Double(frames) * ratio) + 64
            guard let outBuf = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: cap) else { return inBuf }
            var fed = false; var err: NSError?
            conv.convert(to: outBuf, error: &err) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true; status.pointee = .haveData; return inBuf
            }
            return err == nil ? outBuf : inBuf
        }

        func startUtterance() async {
            let t = makeTranscriber()
            let a = SpeechAnalyzer(modules: [t],
                                   options: .init(priority: .userInitiated, modelRetention: .processLifetime))
            let (stream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
            uttStart = Date()
            let col = Task {
                do {
                    for try await r in t.results {
                        let key = r.isFinal ? "final" : "v"
                        emit([key: String(r.text.characters),
                              "t_ms": Int(Date().timeIntervalSince(uttStart) * 1000)])
                    }
                } catch { }
            }
            do { try await a.start(inputSequence: stream) } catch { }
            transcriber = t; analyzer = a; inputCont = cont; collector = col
        }

        // finalize=true（F）：跑完剩餘音訊吐 final；false（R）：丟棄舊語句不吐
        // （reset 必須硬丟棄，否則舊音訊被 finalize 吐出 → 跨語句文字累積，6/13 merge bug）
        func endUtterance(finalize: Bool) async {
            inputCont?.finish()
            if let a = analyzer {
                if finalize { try? await a.finalizeAndFinishThroughEndOfInput() }
                else { await a.cancelAndFinishNow() }
            }
            await collector?.value
            transcriber = nil; analyzer = nil; inputCont = nil; collector = nil
        }

        for await cmd in commands {
            switch cmd {
            case .reset:
                await endUtterance(finalize: false)  // 硬丟棄舊語句
                await startUtterance()
            case .audio(let data):
                if inputCont == nil { await startUtterance() }
                if let buf = pcmBuffer(data) { inputCont?.yield(AnalyzerInput(buffer: buf)) }
            case .finalize:
                await endUtterance(finalize: true)
            }
        }
        await endUtterance(finalize: true)
    }
}
