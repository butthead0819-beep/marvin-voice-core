// macOS STT v2 — SpeechAnalyzer/SpeechTranscriber（macOS 26 新引擎）
// 2026-06-13 spike 驗證：zh-TW 在 supportedLocales、48kHz stereo WAV 直收、
// 455ms/4s 音訊、全程 on-device（無 server 往返）。
//
// CLI 契約與 v1（macos_stt.swift / SFSpeechRecognizer）相同：
//   macos_stt_v2_bin <audio_path>
//   env STT_LOCALE（預設 zh-TW）、STT_CONTEXT_STRINGS（逗號分隔，餵 AnalysisContext）
//   stdout：__META__ {json} 一行 + 最終文字一行
// 差異：confidence 新 API 第一版未暴露 → __META__ 不帶 avg_confidence
//（Python 端 .get() 容忍缺鍵）；標點主動清除（沿 v1 預設無標點的契約）。
//
// 建置：swiftc -parse-as-library macos_stt_v2.swift -o macos_stt_v2_bin
import Foundation
import Speech

@main
struct MacosSTTv2 {
    static func main() async {
        guard CommandLine.arguments.count > 1 else {
            fputs("Error: 請提供音檔路徑做為參數\n", stderr)
            exit(1)
        }
        let audioURL = URL(fileURLWithPath: CommandLine.arguments[1])
        let localeId = ProcessInfo.processInfo.environment["STT_LOCALE"] ?? "zh-TW"

        let supported = await SpeechTranscriber.supportedLocales
        guard let locale = supported.first(where: { $0.identifier(.bcp47) == localeId })
                ?? supported.first(where: { $0.identifier(.bcp47).hasPrefix("zh") }) else {
            fputs("❌ [SwiftV2_Error] 無支援的 locale (\(localeId))\n", stderr)
            exit(1)
        }

        // 2026-06-13b：補 confidence（餵 J1 校準 + stt_confidence 落盤，與 v1 META 同鍵）
        // 與 alternatives（同音字備選證據收集——「週傑倫」的備選常含「周杰倫」）
        let transcriber = SpeechTranscriber(locale: locale,
                                            transcriptionOptions: [],
                                            reportingOptions: [.alternativeTranscriptions],
                                            attributeOptions: [.transcriptionConfidence])

        // 模型資產：未安裝則一次性下載（之後皆本地）
        let installed = await SpeechTranscriber.installedLocales
        if !installed.contains(where: { $0.identifier(.bcp47) == locale.identifier(.bcp47) }) {
            do {
                if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                    fputs("📦 [SwiftV2_Debug] 下載語音模型資產...\n", stdout)
                    try await req.downloadAndInstall()
                }
            } catch {
                fputs("❌ [SwiftV2_Error] 模型資產安裝失敗: \(error)\n", stderr)
                exit(1)
            }
        }

        let analyzer = SpeechAnalyzer(modules: [transcriber])

        // 🚀 [Operation Jargon Override] contextualStrings → AnalysisContext
        if let contextEnv = ProcessInfo.processInfo.environment["STT_CONTEXT_STRINGS"] {
            let strings = contextEnv.components(separatedBy: ",")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
            if !strings.isEmpty {
                let context = AnalysisContext()
                context.contextualStrings[.general] = strings
                do {
                    try await analyzer.setContext(context)
                    fputs("📚 [SwiftV2_Debug] AnalysisContext 注入 \(strings.count) 筆術語。\n", stdout)
                } catch {
                    fputs("⚠️ [SwiftV2_Debug] setContext 失敗（不阻斷）: \(error)\n", stdout)
                }
            }
        }

        guard let audioFile = try? AVAudioFile(forReading: audioURL) else {
            fputs("❌ [SwiftV2_Error] 讀不到音檔\n", stderr)
            exit(1)
        }

        do {
            var segments: [String] = []
            // results 收集與 analyze 並行；finalize 後 stream 結束
            async let collector: ([String], [Double], [String]) = {
                var out: [String] = []
                var confs: [Double] = []
                var alts: [String] = []
                for try await result in transcriber.results {
                    out.append(String(result.text.characters))
                    for run in result.text.runs {
                        if let c = run.transcriptionConfidence, c > 0 {
                            confs.append(c)
                        }
                    }
                    for alt in result.alternatives.prefix(3) {
                        alts.append(String(alt.characters))
                    }
                }
                return (out, confs, alts)
            }()
            if let lastSample = try await analyzer.analyzeSequence(from: audioFile) {
                try await analyzer.finalizeAndFinish(through: lastSample)
            } else {
                await analyzer.cancelAndFinishNow()
            }
            let (segs, confidences, alternatives) = try await collector
            segments = segs

            // 沿 v1 契約：無標點輸出（中文標點歷史上錯斷句、cleaner 會移除）
            func stripPunct(_ s: String) -> String {
                var t = s
                for p in ["。", "，", "！", "？", "；", "、"] {
                    t = t.replacingOccurrences(of: p, with: " ")
                }
                return t.split(separator: " ").joined(separator: " ")
                        .trimmingCharacters(in: .whitespacesAndNewlines)
            }
            // alternativeTranscriptions 開啟後 results 變多段碎片（「馬文播/放週傑倫的晴/天」）。
            // 最終輸出做 CJK 空格收斂：漢字之間的空格全部移除（含標點轉空格的縫），
            // 拉丁詞之間的空格保留。
            func collapseCJKSpaces(_ s: String) -> String {
                let isCJK: (Character) -> Bool = { c in
                    c.unicodeScalars.first.map { $0.value >= 0x4E00 && $0.value <= 0x9FFF } ?? false
                }
                var out: [Character] = []
                let chars = Array(s)
                for (i, c) in chars.enumerated() {
                    if c == " " {
                        let prev = out.last
                        let next = chars[(i + 1)...].first { $0 != " " }
                        if let p = prev, let n = next, isCJK(p) && isCJK(n) { continue }
                    }
                    out.append(c)
                }
                return String(out)
            }
            let text = collapseCJKSpaces(stripPunct(segments.joined(separator: " ")))

            var meta: [String: Any] = [
                "engine": "speechanalyzer",
                "segment_count": segments.count,
            ]
            // confidence 鍵名與 v1 相同 → J1 校準 / stt_confidence 落盤零改動直接生效
            if !confidences.isEmpty {
                meta["avg_confidence"] = confidences.reduce(0, +) / Double(confidences.count)
                meta["min_confidence"] = confidences.min() ?? 0.0
            }
            let cleanAlts = alternatives.map(stripPunct).filter { !$0.isEmpty && $0 != text }
            if !cleanAlts.isEmpty {
                meta["alternatives"] = Array(cleanAlts.prefix(3))
            }
            if let metaData = try? JSONSerialization.data(withJSONObject: meta),
               let metaStr = String(data: metaData, encoding: .utf8) {
                print("__META__ \(metaStr)")
            }
            print(text)
        } catch {
            fputs("❌ [SwiftV2_Error] 轉錄失敗: \(error)\n", stderr)
            exit(1)
        }
    }
}
