// MarvinControl.swift — macOS 選單列 App，馬文 device 原生控制入口。
//
// 鏡射 Pi 上 volume_server.py 的網頁控制台（http://<pi>:8766/）：
//   音量 / 溫度、聲音風格、在家離家、PTT，全走那組乾淨 HTTP+token 端點；
//   點歌 / 說話 / 播放控制走 Mac 大腦 /say。
//
// 常駐右上選單列（LSUIElement，無 Dock 圖示），點一下彈出面板 —— 就是「控制中心」的體感。
// 設定（Pi/Mac 位址、token）存 UserDefaults，可在面板底部展開修改。

import SwiftUI
import AppKit

// MARK: - 設定

enum Cfg {
    static let piKey = "marvinPiBase"
    static let macKey = "marvinMacBase"
    static let tokenKey = "marvinToken"

    static var piBase: String {
        UserDefaults.standard.string(forKey: piKey) ?? "http://100.121.35.41:8766"
    }
    static var macBase: String {
        UserDefaults.standard.string(forKey: macKey) ?? "http://100.123.68.86:8790"
    }
    static var token: String {
        UserDefaults.standard.string(forKey: tokenKey) ?? ""
    }
}

// MARK: - 網路層

struct MarvinClient {
    private func tokenQuery() -> String {
        let t = Cfg.token
        guard !t.isEmpty else { return "" }
        let esc = t.addingPercentEncoding(withAllowedCharacters: .urlQueryValueAllowed) ?? t
        return "t=\(esc)"
    }

    private func request(_ url: URL, method: String = "GET",
                         json: [String: Any]? = nil, text: String? = nil) async throws -> Data {
        var req = URLRequest(url: url, timeoutInterval: 6)
        req.httpMethod = method
        if let json {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: json)
        } else if let text {
            req.setValue("text/plain; charset=utf-8", forHTTPHeaderField: "Content-Type")
            req.httpBody = text.data(using: .utf8)
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return data
    }

    // --- Pi 端 (:8766) ---

    func volState() async throws -> (percent: Int, temp: Double, profile: String) {
        let url = URL(string: "\(Cfg.piBase)/vol?\(tokenQuery())")!
        let data = try await request(url)
        let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return (j["percent"] as? Int ?? -1,
                (j["temp"] as? NSNumber)?.doubleValue ?? -1,
                j["profile"] as? String ?? "")
    }

    @discardableResult
    func setVol(_ value: String) async throws -> Int {
        let esc = value.addingPercentEncoding(withAllowedCharacters: .urlQueryValueAllowed) ?? value
        let url = URL(string: "\(Cfg.piBase)/vol?v=\(esc)&\(tokenQuery())")!
        let data = try await request(url, method: "POST")
        let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return j["percent"] as? Int ?? -1
    }

    func setProfile(_ name: String) async throws {
        let url = URL(string: "\(Cfg.piBase)/profile?\(tokenQuery())")!
        _ = try await request(url, method: "POST", json: ["name": name])
    }

    func presence(_ state: String) async throws {
        let url = URL(string: "\(Cfg.piBase)/presence?\(tokenQuery())")!
        _ = try await request(url, method: "POST", json: ["state": state])
    }

    func presenceStatus() async throws -> String {
        let url = URL(string: "\(Cfg.piBase)/presence?\(tokenQuery())")!
        let data = try await request(url)
        let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return j["status"] as? String ?? ""
    }

    func ptt(_ state: String) async throws {
        let url = URL(string: "\(Cfg.piBase)/ptt?\(tokenQuery())")!
        _ = try await request(url, method: "POST", json: ["state": state])
    }

    // --- Mac 大腦 (:8790) ---

    func say(_ text: String) async throws {
        let url = URL(string: "\(Cfg.macBase)/say?\(tokenQuery())")!
        _ = try await request(url, method: "POST", text: text)
    }

    func nowPlaying() async throws -> (playing: Bool, paused: Bool, title: String, by: String) {
        let url = URL(string: "\(Cfg.macBase)/now?\(tokenQuery())")!
        let data = try await request(url)
        let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return (j["playing"] as? Bool ?? false,
                j["paused"] as? Bool ?? false,
                j["title"] as? String ?? "",
                j["by"] as? String ?? "")
    }
}

extension CharacterSet {
    static let urlQueryValueAllowed: CharacterSet = {
        var cs = CharacterSet.urlQueryAllowed
        cs.remove(charactersIn: "&=?+")
        return cs
    }()
}

// MARK: - ViewModel

@MainActor
final class Model: ObservableObject {
    @Published var percent = -1
    @Published var temp = -1.0
    @Published var profile = ""
    @Published var presence = ""
    @Published var nowTitle = "—"
    @Published var nowBy = ""
    @Published var online = false
    @Published var status = "就緒"
    @Published var pttRecording = false
    @Published var sliderValue = 40.0   // 本地滑桿值（拖曳時不被 refresh 蓋掉）
    @Published var sliderEditing = false

    private let client = MarvinClient()

    func refresh() {
        Task {
            do {
                let v = try await client.volState()
                if v.percent >= 0 {
                    percent = v.percent
                    if !sliderEditing { sliderValue = Double(v.percent) }
                }
                if v.temp > 0 { temp = v.temp }
                if !v.profile.isEmpty { profile = v.profile }
                online = true
            } catch { online = false }
            presence = (try? await client.presenceStatus()) ?? presence
            if let np = try? await client.nowPlaying() {
                if np.playing {
                    nowTitle = (np.paused ? "暫停中 · " : "") + (np.title.isEmpty ? "—" : np.title)
                    nowBy = np.by.isEmpty ? "" : "點播：\(np.by)"
                } else { nowTitle = "沒有播放"; nowBy = "" }
            }
        }
    }

    private func run(_ ok: String, _ fail: String, _ op: @escaping () async throws -> Void) {
        Task {
            do { try await op(); status = ok; refresh() }
            catch { status = fail }
        }
    }

    func commitVolume(_ v: Int) {
        run("音量 \(v)%", "音量服務連不到") { _ = try await self.client.setVol(String(v)) }
    }
    func mute() { run("已靜音", "音量服務連不到") { _ = try await self.client.setVol("mute") } }
    func setProfile(_ name: String) { run("已套用風格：\(name)", "套用風格失敗") { try await self.client.setProfile(name) } }
    func presence(_ state: String) {
        run(state == "off" ? "已離家" : "已到家", "切換失敗") { try await self.client.presence(state) }
    }
    func say(_ text: String) { run("已送出：「\(text)」", "連不到大腦") { try await self.client.say(text) } }

    func togglePTT() {
        let next = pttRecording ? "stop" : "start"
        Task {
            do {
                try await client.ptt(next)
                pttRecording.toggle()
                status = pttRecording ? "🎙️ 錄音中…對著音箱說話" : "⌛ 傳送語音中…"
            } catch { status = "PTT 連不到" }
        }
    }
}

// MARK: - UI

struct Panel: View {
    @ObservedObject var model: Model
    @State private var song = ""
    @State private var cmd = ""
    @State private var showSettings = false
    @AppStorage(Cfg.piKey) private var piBase = "http://100.121.35.41:8766"
    @AppStorage(Cfg.macKey) private var macBase = "http://100.123.68.86:8790"
    @AppStorage(Cfg.tokenKey) private var token = ""

    private let profiles: [(id: String, label: String)] =
        [("calibrated", "校正"), ("pop", "流行"), ("podcast", "播客"), ("spatial", "空間")]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            nowPlaying
            volume
            profileRow
            pttButton
            presenceRow
            quickInputs
            Divider()
            footer
            if showSettings { settings }
        }
        .padding(14)
        .frame(width: 300)
        .onAppear { model.refresh() }
    }

    private var header: some View {
        HStack {
            Circle().fill(model.online ? .green : .red).frame(width: 8, height: 8)
            Text("馬文控制台").font(.headline)
            Spacer()
            if model.temp > 0 {
                Text(String(format: "🌡️ %.1f°C", model.temp))
                    .font(.caption).foregroundStyle(tempColor)
            }
        }
    }
    private var tempColor: Color {
        model.temp >= 65 ? .red : model.temp >= 57 ? .orange : .secondary
    }

    private var nowPlaying: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("🎧 現正播放").font(.caption).foregroundStyle(.secondary)
            Text(model.nowTitle).font(.subheadline).lineLimit(2)
            if !model.nowBy.isEmpty {
                Text(model.nowBy).font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private var volume: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("音量").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text("\(model.percent >= 0 ? "\(model.percent)" : "--")%")
                    .font(.system(.body, design: .rounded).weight(.semibold))
            }
            Slider(value: $model.sliderValue, in: 0...100, step: 1) { editing in
                model.sliderEditing = editing
                if !editing { model.commitVolume(Int(model.sliderValue.rounded())) }
            }
            Button(role: .destructive) { model.mute() } label: {
                Text("靜音").frame(maxWidth: .infinity)
            }
        }
    }

    private var profileRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("🔊 聲音風格").font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 6) {
                ForEach(profiles, id: \.id) { p in
                    Button { model.setProfile(p.id) } label: {
                        Text(p.label).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .tint(model.profile == p.id ? .accentColor : nil)
                }
            }
        }
    }

    private var pttButton: some View {
        Button { model.togglePTT() } label: {
            Text(model.pttRecording ? "🔴 錄音中（點擊結束）" : "🎙️ 開始對話")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .tint(model.pttRecording ? .red : .accentColor)
    }

    private var presenceRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("🏠 在家 / 離家").font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 6) {
                Button(role: .destructive) { model.presence("off") } label: {
                    Text("🔇 離家").frame(maxWidth: .infinity)
                }
                Button { model.presence("on") } label: {
                    Text("🏠 到家").frame(maxWidth: .infinity)
                }
            }
            if !model.presence.isEmpty {
                Text(model.presence).font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private var quickInputs: some View {
        VStack(spacing: 6) {
            HStack {
                TextField("點歌：告白氣球…", text: $song)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(playSong)
                Button("點播", action: playSong)
            }
            HStack(spacing: 6) {
                Button("下一首") { model.say("下一首") }
                Button("暫停") { model.say("暫停播放") }
                Button("繼續") { model.say("繼續播放") }
                Button("停") { model.say("停止播放") }.tint(.red)
            }.buttonStyle(.bordered)
            HStack {
                TextField("說一句話 / 問問題…", text: $cmd)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(sendCmd)
                Button("送出", action: sendCmd)
            }
        }
    }

    private func playSong() {
        let v = song.trimmingCharacters(in: .whitespaces)
        guard !v.isEmpty else { return }
        model.say("放一首" + v); song = ""
    }
    private func sendCmd() {
        let v = cmd.trimmingCharacters(in: .whitespaces)
        guard !v.isEmpty else { return }
        model.say(v); cmd = ""
    }

    private var footer: some View {
        HStack {
            Text(model.status).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            Spacer()
            Button {
                withAnimation { showSettings.toggle() }
            } label: { Image(systemName: "gearshape") }.buttonStyle(.borderless)
            Button("結束") { NSApp.terminate(nil) }.buttonStyle(.borderless).font(.caption2)
        }
    }

    private var settings: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("設定").font(.caption).foregroundStyle(.secondary)
            LabeledField(label: "Pi", text: $piBase)
            LabeledField(label: "Mac", text: $macBase)
            LabeledField(label: "Token", text: $token)
        }
    }
}

struct LabeledField: View {
    let label: String
    @Binding var text: String
    var body: some View {
        HStack {
            Text(label).font(.caption2).frame(width: 42, alignment: .leading)
            TextField("", text: $text).textFieldStyle(.roundedBorder).font(.caption2)
        }
    }
}

// MARK: - App

@main
struct MarvinControlApp: App {
    @StateObject private var model = Model()
    @State private var timer: Timer?

    var body: some Scene {
        MenuBarExtra {
            Panel(model: model)
                .onAppear {
                    timer?.invalidate()
                    timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { _ in
                        Task { @MainActor in model.refresh() }
                    }
                }
        } label: {
            Image(systemName: "hifispeaker.fill")
        }
        .menuBarExtraStyle(.window)
    }
}
