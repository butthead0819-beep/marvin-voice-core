// Volatile Results Phase 0 影子量測（2026-06-13）
// 把一段 utterance WAV 以「實時節奏」（100ms chunks + 100ms sleep）餵進
// SpeechAnalyzer 串流模式，逐筆輸出 volatile/final 假設與 wall-clock 時間，
// 量測：①文字多早趨穩（語意斷句潛在省時）②假設翻盤率。
// 零管線影響：離線重播，不碰 live 音訊路徑。
//
// 輸出（stdout JSONL）：
//   {"t_ms":1234,"start_s":0.0,"end_s":1.2,"fin_s":0.6,"text":"..."}
//   __DONE__ {"audio_ms":4000,"wall_ms":4210}
//
// 建置：swiftc -parse-as-library stream_stt_shadow.swift -o stream_stt_shadow_bin
import Foundation
import Speech
import AVFAudio

func jsonLine(_ obj: [String: Any]) -> String {
    guard let d = try? JSONSerialization.data(withJSONObject: obj),
          let s = String(data: d, encoding: .utf8) else { return "{}" }
    return s
}

@main
struct StreamShadow {
    static func main() async {
        guard CommandLine.arguments.count > 1 else {
            fputs("Error: 請提供音檔路徑\n", stderr); exit(1)
        }
        let url = URL(fileURLWithPath: CommandLine.arguments[1])
        let localeId = ProcessInfo.processInfo.environment["STT_LOCALE"] ?? "zh-TW"

        let supported = await SpeechTranscriber.supportedLocales
        guard let locale = supported.first(where: { $0.identifier(.bcp47) == localeId })
                ?? supported.first(where: { $0.identifier(.bcp47).hasPrefix("zh") }) else {
            fputs("❌ 無支援 locale\n", stderr); exit(1)
        }

        // progressiveTranscription preset：漸進式輸出的官方組合
        // （手工 [.volatileResults] 實測只推進 volatileRange、results 憋到 finalize）
        let transcriber = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)

        guard let file = try? AVAudioFile(forReading: url) else {
            fputs("❌ 讀不到音檔\n", stderr); exit(1)
        }
        let format = file.processingFormat
        let chunkFrames = AVAudioFrameCount(format.sampleRate / 10)  // 100ms
        let audioMs = Int(Double(file.length) / format.sampleRate * 1000)

        // 串流模式不像檔案模式會自動轉格式：必須協商 + 自行 convert，
        // 否則 AnalyzerInput 餵不相容 buffer 直接 SIGTRAP。
        guard let targetFormat = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            fputs("❌ 無相容音訊格式\n", stderr); exit(1)
        }
        guard let converter = AVAudioConverter(from: format, to: targetFormat) else {
            fputs("❌ 建不出格式轉換器 (\(format) → \(targetFormat))\n", stderr); exit(1)
        }

        func convert(_ src: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
            let ratio = targetFormat.sampleRate / format.sampleRate
            let cap = AVAudioFrameCount(Double(src.frameLength) * ratio) + 64
            guard let dst = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: cap) else { return nil }
            var fed = false
            var convErr: NSError?
            converter.convert(to: dst, error: &convErr) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true; status.pointee = .haveData; return src
            }
            return convErr == nil ? dst : nil
        }

        let (stream, continuation) = AsyncStream.makeStream(of: AnalyzerInput.self)
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        // 模型冷載入 ~4.5s 會把「串流時序」量成「尾部爆發」假象：
        // 先暖模型，暖完才起表餵音訊（live 部署時 session 常駐、模型常溫，
        // 量測必須重現的是常溫時序）
        let tPrep = Date()
        try? await analyzer.prepareToAnalyze(in: targetFormat)
        fputs("prepare_ms=\(Int(Date().timeIntervalSince(tPrep) * 1000))\n", stderr)
        let t0 = Date()

        // 收集 task：每筆 result 立即輸出（含 wall-clock 經過毫秒）
        let collector = Task {
            do {
                for try await result in transcriber.results {
                    let line = jsonLine([
                        "t_ms": Int(Date().timeIntervalSince(t0) * 1000),
                        "start_s": result.range.start.seconds,
                        "end_s": result.range.end.seconds,
                        "fin_s": result.resultsFinalizationTime.seconds,
                        "text": String(result.text.characters),
                    ])
                    print(line)
                    fflush(stdout)
                }
            } catch {
                fputs("collector error: \(error)\n", stderr)
            }
        }

        // volatile 時間軸探針：若餵入期間從不觸發＝analyzer 沒在邊餵邊處理
        await analyzer.setVolatileRangeChangedHandler { range, _, _ in
            fputs("volatile_range t=\(Int(Date().timeIntervalSince(t0) * 1000))ms range=\(range.start.seconds)-\(range.end.seconds)\n", stderr)
        }

        // 餵入 task：100ms chunk + 實時 sleep（重現 live 節奏）+ 顯式 bufferStartTime
        let feeder = Task {
            file.framePosition = 0
            var fedFrames: Double = 0
            while file.framePosition < file.length {
                guard let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: chunkFrames) else { break }
                do { try file.read(into: buf, frameCount: chunkFrames) } catch { break }
                if buf.frameLength == 0 { break }
                if let converted = convert(buf) {
                    // bufferStartTime=nil（連續語意）：顯式時間戳 + 重採樣的微縫隙
                    // 會讓 analyzer 停在 0.2s 等補洞（實測 stall）
                    continuation.yield(AnalyzerInput(buffer: converted))
                }
                fedFrames += Double(buf.frameLength)
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            continuation.finish()
        }

        do {
            // start = 自主即時分析（邊餵邊出 volatile）；analyzeSequence 是批次語意，
            // 實測會把所有結果憋到 finalize 一次爆出，量不到串流時序。
            try await analyzer.start(inputSequence: stream)
            await feeder.value
            try await analyzer.finalizeAndFinishThroughEndOfInput()
            await collector.value
            print("__DONE__ " + jsonLine([
                "audio_ms": audioMs,
                "wall_ms": Int(Date().timeIntervalSince(t0) * 1000),
            ]))
        } catch {
            fputs("❌ 串流分析失敗: \(error)\n", stderr)
            exit(1)
        }
    }
}
