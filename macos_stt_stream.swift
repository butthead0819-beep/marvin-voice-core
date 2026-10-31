// macOS 常駐串流 STT CLI — SpeechAnalyzer/SpeechTranscriber，stdin 收 PCM framing、
// stdout 吐 JSON lines（ready / result）。locale/資產下載/AnalysisContext 寫法照抄
// macos_stt_v2.swift；差異是這支不吃檔案、是長駐程序，靠二進位 framing 收串流音訊。
//
// 建置：swiftc -O -parse-as-library macos_stt_stream.swift -o macos_stt_stream_bin
//
// stdin 協定：每則訊息 = 1 byte type + uint32 LE length + payload
//   'A' (0x41)：payload = 16kHz mono int16 LE PCM（length 必為偶數）
//   'F' (0x46)：length=0，VAD 段落結束 → 背景 finalize 目前已餵的音訊（不擋讀取，見 case 0x46）
//   EOF：finalizeAndFinishThroughEndOfInput() 後等 results 收完、exit 0
//
// stdout 協定（JSON lines，每行 flush）：
//   {"type":"ready"}
//   {"type":"result","final":bool,"start":秒,"end":秒,"text":"...","runs":[["...",start,end],...]}
import Foundation
import Speech
import AVFoundation
import CoreMedia

func emitJSONLine(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj),
          let str = String(data: data, encoding: .utf8) else { return }
    print(str)
    fflush(stdout)
}

func emitResult(_ result: SpeechTranscriber.Result) {
    let text = String(result.text.characters)
    let start = CMTimeGetSeconds(result.range.start)
    let end = CMTimeGetSeconds(result.range.end)

    var runs: [[Any]] = []
    for run in result.text.runs {
        let runText = String(result.text[run.range].characters)
        if let timeRange = run.audioTimeRange {
            runs.append([runText, CMTimeGetSeconds(timeRange.start), CMTimeGetSeconds(timeRange.end)])
        } else {
            runs.append([runText, NSNull(), NSNull()])
        }
    }

    emitJSONLine([
        "type": "result",
        "final": result.isFinal,
        "start": start,
        "end": end,
        "text": text,
        "runs": runs,
    ])
}

@main
struct MacosSTTStream {
    static func main() async {
        setvbuf(stdout, nil, _IOLBF, 0)

        let localeId = ProcessInfo.processInfo.environment["STT_LOCALE"] ?? "zh-TW"
        let supported = await SpeechTranscriber.supportedLocales
        guard let locale = supported.first(where: { $0.identifier(.bcp47) == localeId })
                ?? supported.first(where: { $0.identifier(.bcp47).hasPrefix("zh") }) else {
            fputs("❌ [SwiftStream_Error] 無支援的 locale (\(localeId))\n", stderr)
            exit(1)
        }

        let transcriber = SpeechTranscriber(locale: locale,
                                             transcriptionOptions: [],
                                             reportingOptions: [.volatileResults, .fastResults],
                                             attributeOptions: [.audioTimeRange])

        let installed = await SpeechTranscriber.installedLocales
        if !installed.contains(where: { $0.identifier(.bcp47) == locale.identifier(.bcp47) }) {
            do {
                if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                    fputs("📦 [SwiftStream_Debug] 下載語音模型資產...\n", stderr)
                    try await req.downloadAndInstall()
                }
            } catch {
                fputs("❌ [SwiftStream_Error] 模型資產安裝失敗: \(error)\n", stderr)
                exit(1)
            }
        }

        let analyzer = SpeechAnalyzer(modules: [transcriber])

        if let contextEnv = ProcessInfo.processInfo.environment["STT_CONTEXT_STRINGS"] {
            let strings = contextEnv.components(separatedBy: ",")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
            if !strings.isEmpty {
                let context = AnalysisContext()
                context.contextualStrings[.general] = strings
                do {
                    try await analyzer.setContext(context)
                } catch {
                    fputs("⚠️ [SwiftStream_Debug] setContext 失敗（不阻斷）: \(error)\n", stderr)
                }
            }
        }

        guard let target = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            fputs("❌ [SwiftStream_Error] 無可用音訊格式\n", stderr)
            exit(1)
        }
        guard let inputFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                               channels: 1, interleaved: true) else {
            fputs("❌ [SwiftStream_Error] 無法建立輸入格式\n", stderr)
            exit(1)
        }
        let converter: AVAudioConverter? = (target == inputFormat) ? nil : AVAudioConverter(from: inputFormat, to: target)

        do {
            try await analyzer.prepareToAnalyze(in: target)
        } catch {
            fputs("❌ [SwiftStream_Error] prepareToAnalyze 失敗: \(error)\n", stderr)
            exit(1)
        }

        let (inputStream, continuation) = AsyncStream<AnalyzerInput>.makeStream()

        do {
            try await analyzer.start(inputSequence: inputStream)
        } catch {
            fputs("❌ [SwiftStream_Error] analyzer.start 失敗: \(error)\n", stderr)
            exit(1)
        }

        let resultsTask = Task {
            do {
                for try await result in transcriber.results {
                    emitResult(result)
                }
            } catch {
                fputs("⚠️ [SwiftStream_Debug] results stream 結束: \(error)\n", stderr)
            }
        }

        emitJSONLine(["type": "ready"])

        let ioTask = Task.detached {
            let stdinHandle = FileHandle.standardInput
            var fedSamples: Int64 = 0

            func readExact(_ n: Int) -> Data? {
                guard n > 0 else { return Data() }
                var data = Data()
                while data.count < n {
                    let chunk = stdinHandle.readData(ofLength: n - data.count)
                    if chunk.isEmpty { return nil }
                    data.append(chunk)
                }
                return data
            }

            func makeInputBuffer(_ payload: Data) -> AVAudioPCMBuffer? {
                let sampleCount = payload.count / 2
                guard sampleCount > 0,
                      let buffer = AVAudioPCMBuffer(pcmFormat: inputFormat,
                                                     frameCapacity: AVAudioFrameCount(sampleCount)) else {
                    return nil
                }
                buffer.frameLength = AVAudioFrameCount(sampleCount)
                payload.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
                    let src = raw.bindMemory(to: Int16.self)
                    if let dst = buffer.int16ChannelData?[0], let base = src.baseAddress {
                        dst.update(from: base, count: sampleCount)
                    }
                }
                return buffer
            }

            func convertIfNeeded(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
                guard let converter = converter else { return buffer }
                let ratio = target.sampleRate / inputFormat.sampleRate
                let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 16
                guard let outBuf = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }
                var provided = false
                var convError: NSError?
                converter.convert(to: outBuf, error: &convError) { _, outStatus in
                    if provided {
                        outStatus.pointee = .noDataNow
                        return nil
                    }
                    provided = true
                    outStatus.pointee = .haveData
                    return buffer
                }
                if let convError = convError {
                    fputs("⚠️ [SwiftStream_Debug] 音訊轉換失敗: \(convError)\n", stderr)
                    return nil
                }
                return outBuf
            }

            readLoop: while true {
                guard let typeData = readExact(1) else { break readLoop }
                guard let lenData = readExact(4) else { break readLoop }
                let length = lenData.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> UInt32 in
                    UInt32(littleEndian: raw.load(as: UInt32.self))
                }

                switch typeData[0] {
                case 0x41: // 'A'
                    guard length % 2 == 0 else {
                        fputs("⚠️ [SwiftStream_Debug] 'A' frame 長度非偶數（\(length)），跳過\n", stderr)
                        guard readExact(Int(length)) != nil else { break readLoop }
                        continue readLoop
                    }
                    guard let payload = readExact(Int(length)) else { break readLoop }
                    guard length > 0, let rawBuffer = makeInputBuffer(payload) else { continue readLoop }
                    guard let finalBuffer = convertIfNeeded(rawBuffer) else { continue readLoop }
                    let startTime = CMTime(value: fedSamples, timescale: 16000)
                    continuation.yield(AnalyzerInput(buffer: finalBuffer, bufferStartTime: startTime))
                    fedSamples += Int64(rawBuffer.frameLength)

                case 0x46: // 'F'
                    if length > 0 {
                        fputs("⚠️ [SwiftStream_Debug] 'F' frame 長度非 0（\(length)），跳過 payload\n", stderr)
                        guard readExact(Int(length)) != nil else { break readLoop }
                    }
                    // 2026-10-31 實測：在讀取迴圈裡 await finalize 會死結（它要等後面的輸入，
                    // 但讀取迴圈被它擋住、不再 yield）。另開 Task 跑，不擋讀取；照即時節奏餵時
                    // 約 3s 後回 final（帶逐字時間）。
                    if fedSamples > 0 {
                        let through = CMTime(value: fedSamples, timescale: 16000)
                        Task.detached {
                            do {
                                try await analyzer.finalize(through: through)
                            } catch {
                                fputs("⚠️ [SwiftStream_Debug] finalize 失敗: \(error)\n", stderr)
                            }
                        }
                    }

                default:
                    fputs("⚠️ [SwiftStream_Debug] 未知 frame type 0x\(String(typeData[0], radix: 16))，跳過\n", stderr)
                    if length > 0 {
                        guard readExact(Int(length)) != nil else { break readLoop }
                    }
                }
            }

            continuation.finish()
            do {
                try await analyzer.finalizeAndFinishThroughEndOfInput()
            } catch {
                fputs("⚠️ [SwiftStream_Debug] finalizeAndFinishThroughEndOfInput 失敗: \(error)\n", stderr)
            }
        }

        await ioTask.value
        await resultsTask.value
        exit(0)
    }
}
