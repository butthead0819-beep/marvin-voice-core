import Foundation
import Speech

// 📝 [Debug] 獲取目前授權狀態的輔助函式
func getAuthStatusString(_ status: SFSpeechRecognizerAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: return "Not Determined (未決定)"
    case .denied: return "Denied (被拒絕)"
    case .restricted: return "Restricted (受限制)"
    case .authorized: return "Authorized (已授權)"
    @unknown default: return "Unknown (未知)"
    }
}

// 1. 解析命令列參數
// 用法：macos_stt_bin <audio_path> [--wake-check]
//   --wake-check  使用 On-Device 模型（無網路延遲，適合 wake word 偵測）
guard CommandLine.arguments.count > 1 else {
    fputs("Error: 請提供音檔路徑做為參數\n", stderr)
    exit(1)
}

let audioPath = CommandLine.arguments[1]
let audioURL = URL(fileURLWithPath: audioPath)
let isWakeCheck = CommandLine.arguments.contains("--wake-check")

// 2. 檢查授權狀態
let authStatus = SFSpeechRecognizer.authorizationStatus()
fputs("🔍 [Swift_Debug] 目前授權狀態: \(getAuthStatusString(authStatus))\n", stdout)

// 3. 語系設定：優先讀取 STT_LOCALE 環境變數，預設 zh-TW
let localeId = ProcessInfo.processInfo.environment["STT_LOCALE"] ?? "zh-TW"
let locale = Locale(identifier: localeId)
guard let recognizer = SFSpeechRecognizer(locale: locale) else {
    fputs("❌ [Swift_Error] 此設備不支援指定的語音辨識語系 (\(localeId))\n", stderr)
    exit(1)
}

fputs("🔍 [Swift_Debug] 辨識器就緒 (Locale: \(locale.identifier), isAvailable: \(recognizer.isAvailable))\n", stdout)

if !recognizer.isAvailable {
    fputs("❌ [Swift_Error] 語音辨識服務目前無法使用\n", stderr)
    exit(1)
}

let request = SFSpeechURLRecognitionRequest(url: audioURL)
request.shouldReportPartialResults = false // 關閉部分結果，專注於最終產出
// Wake check 使用 On-Device 模型：無網路延遲，省去 100-300ms Apple 伺服器往返。
// 完整句使用 Server 模型：準確度更高，適合指令理解。
request.requiresOnDeviceRecognition = isWakeCheck

if #available(macOS 13.0, *) {
    request.taskHint = .dictation // 明確告訴辨識引擎這是即時對話，非朗讀
    // 標點預設關（過往實測：Apple 中文標點會錯斷句、且 cleaner 經常移除多加的標點）。
    // 留 STT_PUNCTUATION=1 後門 A/B 測；wake-check 路徑搶速度，強制關不受 env 影響。
    let punctEnv = ProcessInfo.processInfo.environment["STT_PUNCTUATION"] ?? "0"
    request.addsPunctuation = !isWakeCheck && (punctEnv == "1" || punctEnv.lowercased() == "true")
}

// 🚀 [Operation Jargon Override] 讀取環境變數載入動態字典
if let contextEnv = ProcessInfo.processInfo.environment["STT_CONTEXT_STRINGS"] {
    let contextStrings = contextEnv.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    if !contextStrings.isEmpty {
        request.contextualStrings = contextStrings
        fputs("📚 [Swift_Debug] 成功讀取動態黑話字典，共注入 \(contextStrings.count) 筆術語。\n", stdout)
    }
}

fputs("🔍 [Swift_Debug] 準備開始辨識任務 (URL: \(audioURL.lastPathComponent), Auto-Engine Mode)...\n", stdout)

var isDone = false

let task = recognizer.recognitionTask(with: request) { result, error in
    if let error = error {
        fputs("❌ [Swift_Error] 任務失敗: \(error.localizedDescription)\n", stdout)
        fputs("STT Error Trace: \(error.localizedDescription)\n", stderr)
        isDone = true
        return
    }
    
    if let result = result {
        if result.isFinal {
            let transcription = result.bestTranscription
            let finalStr = transcription.formattedString
            fputs("✅ [Swift_Debug] 辨識成功！內容長度: \(finalStr.count)\n", stdout)

            // META line: acoustic + prosody features for J1 calibration / VAD temperature.
            // Confidence is per-segment Float (0.0–1.0); a value of 0.0 means "no estimate"
            // (common on short utterances) — filter those out before averaging so wake-check
            // doesn't get penalised.
            let confidences = transcription.segments.map { $0.confidence }.filter { $0 > 0 }
            let avgConf = confidences.isEmpty ? 0.0 : confidences.reduce(0, +) / Float(confidences.count)
            let minConf = confidences.min() ?? 0.0
            var meta: [String: Any] = [
                "avg_confidence": Double(avgConf),
                "min_confidence": Double(minConf),
                "segment_count": transcription.segments.count,
            ]
            // Prosody (pause / rate) lives on result.speechRecognitionMetadata since macOS 11.3.
            // It can be nil when the engine doesn't compute it (short clips, on-device path) —
            // Python uses .get() so missing keys are fine downstream.
            if let speechMeta = result.speechRecognitionMetadata {
                meta["avg_pause_duration"] = speechMeta.averagePauseDuration
                meta["speaking_rate"] = speechMeta.speakingRate
            }
            if let metaData = try? JSONSerialization.data(withJSONObject: meta, options: []),
               let metaStr = String(data: metaData, encoding: .utf8) {
                print("__META__ \(metaStr)")
            }
            print(finalStr)
            isDone = true
        }
    }
}

// ⚠️ 運行 RunLoop 確保非同步回呼執行
let startTime = Date()
let timeoutSeconds: TimeInterval = 15

while !isDone {
    RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.1))
    if Date().timeIntervalSince(startTime) > timeoutSeconds {
        fputs("❌ [Swift_Error] STT 辨識逾時 (\(Int(timeoutSeconds))s)\n", stderr)
        task.cancel()
        exit(1)
    }
}

_ = task