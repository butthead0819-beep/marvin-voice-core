# Design: Marvin ESP32 Car Puck 極致微型化 (Ø42mm) 雙層板硬體與外殼規格書

- Status: READY FOR REVIEW / DFM AUDIT
- Target Audience: Claude Code / Hardware Reviewer / PCB Layout Engineer
- Target Hardware:
  - `PCB Board A (Ø38mm UI Panel)`: GC9A01 圓屏 + INMP441 麥克風 + PTT 按鈕
  - `PCB Board B (Ø38mm Main Core)`: ESP32-S3-MINI-1-N8R8 + ESP32-PICO-V3-02 + SY8089 降壓 + 6點鐘 Type-C
  - `Enclosure (Ø42mm Cylindrical Puck)`: JLC3DP HP PA12 尼龍染黑 (前蓋 + 底蓋)
  - `Power System`: 6點鐘垂直 Type-C + Apple Watch 規格 (Ø28mm) 微型磁吸無線供電
- Firmware Reference:
  - [`firmware/car_puck/car_puck.ino`](file:///Users/jackhuang/Code/Discord-voice-bot/firmware/car_puck/car_puck.ino)
  - [`firmware/bt_bridge/bt_bridge.ino`](file:///Users/jackhuang/Code/Discord-voice-bot/firmware/bt_bridge/bt_bridge.ino)
  - [`docs/hardware/car_puck_schematic.html`](file:///Users/jackhuang/Code/Discord-voice-bot/docs/hardware/car_puck_schematic.html)
- Branch: main
- Priority: P1

---

## 一、 設計背景與極限約束（Design Philosophy & Constraints）

### 1.1 為什麼必須是 Ø42mm 純圓柱直筒？
- **出風口零遮擋**：車載冷氣出風口直徑通常僅 65~75mm。傳統車用 MagSafe 支架外徑超過 60mm，會將出風口完全擋死。將 Puck 控制在 **Ø42.0mm**，能確保冷暖氣正常循環流通。
- **純直筒不變大**：拒絕底盤向外擴展至 55mm（飛碟裙邊），維持如一枚厚硬幣般純粹、俐落的極簡幾何輪廓（直徑 Ø42.0mm、總厚度 14.0mm）。
- **取消杜邦線**：徹底消滅目前車載環境中容易鬆脫、產生 40MHz SPI 破圖雜訊與 I2S 音訊 Jitter 爆音的杜邦線束。

### 1.2 雙晶片架構的不可替代性
- **ESP32-S3**：負責 WiFi 串流、MP3 軟解碼 (Helix)、24fps GC9A01 圓形面部旋轉渲染 (face_rt)、INMP441 錄音與語音上傳。**（S3 只有 BLE，不支援 Classic BT A2DP）**。
- **ESP32-D0WD (PICO-V3-02)**：專職作為車載 A2DP Source 發射器，連線 BMW / 車載主機與藍牙喇叭，並透過 AVRCP 回傳曲名中繼資料與接收方向盤按鍵。

---

## 二、 雙層夾心板 (Sandwich PCB) 架構與空間分配

為了在 Ø42.0mm 外殼內塞入兩顆 MCU、音訊、螢幕與電源，系統拆解為兩片相互平行的圓形 PCB（板間透過 1.27mm 微型排針對插，淨空高度 4.5mm）：

```
           [前蓋 Top Bezel] (外徑 Ø42.0mm，帶 Ø33mm 螢幕窗)
                  │
     ┌────────────┴────────────┐
     │ 1.28" GC9A01 圓形液晶面板 │ (可視區 Ø32.4mm，厚度 1.6mm)
     └────────────┬────────────┘
                  │
┌─────────────────┴─────────────────┐
│ Board A：頂層面板 (UI Panel)       │ (直徑 Ø38.0mm，厚度 1.0mm，2 層板)
│ 正面：GC9A01 (FPC座) + INMP441 麥 │
│ 背面：1.27mm 排母 (12-Pin) + PTT  │
└─────────────────┬─────────────────┘
                  │ 1.27mm 雙排針 (垂直高度 4.5mm)
┌─────────────────┴─────────────────┐
│ Board B：底層主板 (Main Core)     │ (直徑 Ø38.0mm，厚度 1.0mm，4 層板)
│ 正面：S3-MINI + PICO + 降壓電路   │
│ 背面：6點鐘中沉 Type-C + Qi頂針   │
└─────────────────┬─────────────────┘
                  │
┌─────────────────┴─────────────────┐
│ Qi 接收層：隔磁片 + 手錶級線圈     │ (直徑 Ø26.0mm，厚度 0.6mm，5V 500mA)
│ 磁吸層：手錶級引鐵片              │ (外徑 Ø28.0mm，厚度 0.5mm)
└─────────────────┬─────────────────┘
                  │
           [底蓋 Bottom Case] (外徑 Ø42.0mm，底面開 Ø28.5mm 手錶充電凹槽)
```

---

## 三、 兩板對接 12-Pin 微型排針 (Inter-Board Pinout)

Board A 與 Board B 透過一組 **1.27mm 間距雙排針 (2×6 或 1×12)** 互連。板間淨空 **4.5mm**，避開 Board B 正面的 S3-MINI 屏蔽蓋高度（3.1mm）。

| Pin | 訊號名稱 | 訊號方向 | 電氣特性 | 用途說明 |
|---|---|---|---|---|
| **1** | `VCC_3V3` | Board B ➔ Board A | 電源 (3.3V 2A 軌) | 供應螢幕、INMP441、RGB LED |
| **2** | `GND` | 共地 | 數位地平面 | 訊號回流與屏蔽 |
| **3** | `SPI_CLK` | Board B ➔ Board A | 40MHz SPI SCLK | S3 GPIO 21，GC9A01 時脈 |
| **4** | `SPI_MOSI` | Board B ➔ Board A | 40MHz SPI MOSI | S3 GPIO 47，螢幕像素資料 |
| **5** | `TFT_DC` | Board B ➔ Board A | GPIO 輸出 | S3 GPIO 40，命令/資料切換 |
| **6** | `TFT_CS` | Board B ➔ Board A | GPIO 輸出 | S3 GPIO 41，螢幕片選 |
| **7** | `TFT_RST` | Board B ➔ Board A | GPIO 輸出 | S3 GPIO 45，螢幕硬體重置 |
| **8** | `TFT_BLK` | Board B ➔ Board A | 44.1kHz PWM | S3 GPIO 42，背光亮度控制 |
| **9** | `MIC_SCK` | Board B ➔ Board A | I2S BCLK | S3 GPIO 5，INMP441 時脈 |
| **10** | `MIC_WS` | Board B ➔ Board A | I2S LRCLK | S3 GPIO 4，音訊框選 (16kHz) |
| **11** | `MIC_SD` | Board A ➔ Board B | I2S DATA | S3 GPIO 6，麥克風錄音輸入 |
| **12** | `PTT_KEY` | Board A ➔ Board B | 數位輸入 (上拉) | S3 GPIO 0，PTT 喚醒按鈕 (按下接地) |

---

## 四、 雙晶片選型驗證（審查核對重點）

### 4.1 主控：ESP32-S3-MINI-1-N8R8
- **封裝尺寸**：$16.6\text{ mm} \times 13.2\text{ mm} \times 2.4\text{ mm}$（比 WROOM-1 縮小 52%）。
- **關鍵規格**：
  - 核心：Xtensa 32-bit LX7 雙核 240MHz。
  - Flash：8MB SPI Flash。
  - **PSRAM：8MB Octal (OPI) PSRAM**。
- ⚠️ **Reviewer 檢查項**：
  - 必須嚴格鎖定料號為 `ESP32-S3-MINI-1-N8R8`（LCSC `C5444315`）。
  - **嚴禁使用 N4R2**（只有 2MB Quad PSRAM，目前韌體的 MP3 1MB 環形緩衝與 240px 封面圖會直接導致 OOM 崩潰）。

### 4.2 藍牙發射：ESP32-PICO-V3-02
- **封裝尺寸**：$7.0\text{ mm} \times 7.0\text{ mm} \times 1.11\text{ mm}$ (QFN-48)。
- **關鍵規格**：
  - 內建晶片：原版 ESP32-D0WD-V3（具備 Classic Bluetooth 4.2 BR/EDR）。
  - Flash：8MB SPI Flash 封裝在晶片內。
  - 外部天線：引出 Pin 2 (ANT)，在 PCB 上串接匹配電感並連接至 **3216 陶瓷貼片天線 (LCSC C96356)**。
- ⚠️ **Reviewer 檢查項**：
  - 目前韌體 [`firmware/bt_bridge/bt_bridge.ino`](file:///Users/jackhuang/Code/Discord-voice-bot/firmware/bt_bridge/bt_bridge.ino) 的 A2DP Source 與 `-Wl,--wrap` AVRCP 函式庫完全相容，零修改直接跑。
  - **嚴禁替換為 ESP32-C3 / C6**（這兩者僅有 BLE，不具備 Classic BT A2DP Profile，會完全無法連線車載車機）。

---

## 五、 電源系統：6點鐘 Type-C + Apple Watch 微型磁吸無線供電

### 5.1 功耗預算 (Power Budget)
- ESP32-S3-MINI (WiFi 傳輸 + 24fps 渲染 + 解碼)：約 250mA @ 3.3V
- ESP32-PICO (Classic BT A2DP 發射)：約 150mA @ 3.3V
- GC9A01 螢幕背光 (全亮)：約 40mA @ 3.3V
- **系統總峰值電流**：約 550mA @ 3.3V（約 1.8W）
- **平均連續功耗**：約 5V / 0.45A（約 2.25W）

### 5.2 Apple Watch 磁吸供電整合（直徑死守 Ø42mm 的關鍵）
- **發射端支架**：相容市售標準「車載 Apple Watch 磁吸出風口充電支架」（發射盤直徑約 Ø28mm）。
- **接收端模組**：
  - 選用專為穿戴設備設計的 **微型手錶 5V 接收貼片（直徑 Ø26.0mm，厚度 0.6mm）**。
  - 線圈背面自帶 0.25mm 鐵氧體隔磁片（Ferrite Sheet），防止交變磁場干擾 Board B 地銅皮。
  - 輸出額定：5V / 500mA ~ 800mA（2.5W ~ 4W），完全滿足系統 2.25W 需求。
- **雙電源自動切換 (Power Path)**：
  - Type-C 5V 輸入端經肖特基二極體 (SS34, 3A / 40V) 連接至內部 `VBUS_SYS`。
  - Qi 接收端 5V 同樣經肖特基二極體連接至 `VBUS_SYS`。
  - 插線時 Type-C 優先供電（電壓略高），拔線時無縫切換至 Qi 磁吸取電，MCU 不斷電重開。

---

## 六、 完整 JLCPCB SMT 貼片 BOM 清單 (LCSC Part Numbers)

整套電路極致精簡，所有被動元件優先採用 JLCPCB Basic Parts（基礎庫，免收每盤 $3 USD 換料費）：

| 標號 (Ref) | 元件名稱 | 封裝規格 | LCSC Part # | 庫別 (Library) | 數量 | 功能用途 |
|---|---|---|---|---|---|---|
| **U1** | 主控 MCU | ESP32-S3-MINI-1-N8R8 | **C5444315** | Extended | 1 | WiFi、MP3解碼、24fps頭部渲染、語音錄音 |
| **U2** | 藍牙 MCU | ESP32-PICO-V3-02 (QFN-48) | **C711382** | Extended | 1 | Classic BT A2DP 發射、AVRCP 曲名 |
| **ANT1** | 2.4G 天線 | 陶瓷貼片天線 (3.2×1.6mm) | **C96356** | **Basic** | 1 | PICO 藍牙射頻天線 |
| **U3** | 降壓 DC-DC | SY8089AAAC (SOT-23-5) | **C78990** | **Basic** | 1 | 5V 轉 3.3V 2A 供電 |
| **J1** | 6點鐘插座 | TYPE-C 16P 中沉板 0.8mm | **C165948** | Extended | 1 | 垂直出線 5V 供電與 S3 燒錄 |
| **J2** | 螢幕插座 | FPC-0.5mm-8P 臥貼下接 | **C262657** | Extended | 1 | 直插 GC9A01 8-Pin 軟排 |
| **SW1** | PTT 按鍵 | 3×6×2.5mm SMD 輕觸按鈕 | **C139797** | **Basic** | 1 | PTT 喚醒通話鍵 (接 S3 GPIO 0) |
| **LED1** | 狀態 RGB | WS2812B-B (3535 / 2020) | **C114587** | Extended | 1 | 系統狀態呼吸燈 (接 S3 GPIO 48) |
| **D1, D2** | 防倒灌二極體| SS34 (SMA 封裝, 3A 40V) | **C8678** | **Basic** | 2 | Type-C 與 Qi 雙電源自動切換 |
| **L1** | 功率電感 | 2.2µH SMD 屏蔽功率電感 | **C2932918** | **Basic** | 1 | SY8089 降壓儲能電感 (>2.5A) |
| **C1, C2** | 輸入輸出電容| 10µF 0805 16V X5R | **C15850** | **Basic** | 4 | 電源穩壓去耦濾波 |
| **C3~C6** | 高頻去耦 | 100nF (0.1µF) 0603 50V | **C14663** | **Basic** | 4 | 晶片電源腳高頻旁路 |
| **R1, R2** | CC 下拉電阻 | 5.1kΩ 0603 1% | **C23186** | **Basic** | 2 | Type-C CC1/CC2 辨識，啟用 5V |
| **R3** | 上拉電阻 | 10kΩ 0603 1% | **C25804** | **Basic** | 1 | PTT 訊號硬體上拉 |
| **PAD1,2** | Qi 焊盤 | Ø2.0mm 鍍金大圓焊盤 | PCB 裸銅 | - | 2 | 連接微型 Qi 線圈 5V 與 GND |

---

## 七、 3D 外殼參數規格與 OpenSCAD 腳本（純直筒 Ø42.0mm）

### 7.1 機構參數清單
- **材質製程**：JLC3DP - **HP Multi Jet Fusion (MJF) PA12 尼龍（Black Dyed 染黑）**。
- **前蓋 (Top Bezel)**：
  - 外徑：`Ø42.0 mm`
  - 總高：`6.5 mm`
  - 螢幕視窗開口：`Ø33.0 mm`（完整露出 GC9A01 可視區，遮蔽邊緣黑框）
  - 螢幕內沉台：`Ø36.0 mm` × 深度 `1.6 mm`
  - 麥克風拾音孔：`Ø1.5 mm`（位於 10 點鐘方向）
  - 緊固件：3 個 M2 沉頭螺絲孔（分佈於 `Ø38.5 mm` 圓周，120° 等距）
- **底蓋 (Bottom Case)**：
  - 外徑：`Ø42.0 mm`（純直筒，無任何向外擴展）
  - 總高：`8.0 mm`（內部淨空 6.2 mm）
  - **底面手錶磁吸沉槽**：`Ø28.5 mm` × 深度 `1.2 mm`（外側卡扣 Apple Watch 充電座，內側貼線圈）
  - **正下方 (6點鐘) Type-C 槽**：寬 `12.0 mm` × 高 `6.5 mm`（帶 0.8mm 深導角）
  - 內部固定柱：3 個 M2 螺紋銅柱槽（`Ø3.2 mm` × 深度 `3.8 mm`，供熱熔螺母嵌入）

### 7.2 參數化 OpenSCAD 腳本（一鍵生成 STL）

```openscad
// ========================================================
// Marvin Car Puck — Ø42mm 極致微型化外殼 (OpenSCAD 腳本)
// 相容 Apple Watch 磁吸無線充底座 + 正下方 Type-C
// ========================================================
$fn = 120;

OD = 42.0;       // 外徑 42mm
PCB_D = 38.6;    // 容納 Ø38mm PCB 的內徑 (含 0.6mm 公差)

module top_bezel() {
    difference() {
        cylinder(d=OD, h=6.5);
        translate([0, 0, -1]) cylinder(d=33.0, h=10);        // 螢幕開窗
        translate([0, 0, 4.9]) cylinder(d=36.0, h=2.0);      // 螢幕內沉槽
        translate([-11, 11, -1]) cylinder(d=1.5, h=10);      // 麥克風孔
        for(a=[0, 120, 240]) {
            rotate([0, 0, a]) translate([19.25, 0, -1]) cylinder(d=2.2, h=10); // M2 通孔
        }
    }
}

module bottom_case() {
    difference() {
        cylinder(d=OD, h=8.0);
        // 內部空間 (放底板與夾層)
        translate([0, 0, 2.0]) cylinder(d=PCB_D, h=8.0);
        // 底面 Apple Watch 規格微凹沉槽 (Ø28.5mm, 深 1.2mm)
        translate([0, 0, -0.1]) cylinder(d=28.5, h=1.3);
        // 6點鐘正下方 Type-C 出線槽
        translate([-6.0, -22.0, 2.0]) cube([12.0, 10.0, 6.5]);
    }
    // 3 根內部螺絲柱
    for(a=[0, 120, 240]) {
        rotate([0, 0, a]) translate([19.25, 0, 0]) difference() {
            cylinder(d=5.0, h=6.5);
            translate([0, 0, 2.5]) cylinder(d=3.2, h=5.0); // M2 熱熔螺母孔
        }
    }
}

// 平鋪渲染，按 F6 算圖後分別匯出 STL
translate([-25, 0, 0]) top_bezel();
translate([25, 0, 0]) bottom_case();
```

---

## 八、 審查與驗收檢查清單 (Review Checklist for Claude Code)

- [ ] **PSRAM 規格驗證**：U1 確認為 `ESP32-S3-MINI-1-N8R8`，具備 8MB Octal PSRAM，滿足 JPEG 與 MP3 緩衝。
- [ ] **藍牙協議相容性**：U2 確認為 `ESP32-PICO-V3-02`，具備 Classic Bluetooth 4.2 A2DP，非僅支援 BLE 的 C3/C6。
- [ ] **天線隔離度**：S3 板載天線朝向 12 點鐘，PICO 陶瓷天線朝向 3 點鐘或 9 點鐘，兩者相距 >25mm，且天線正下方嚴禁走線與鋪銅（Keep-out）。
- [ ] **高頻走線阻抗**：Board A 到 Board B 的 SPI 時脈 (40MHz) 走線長度控制在 25mm 以內。
- [ ] **Qi 隔磁片**：確認微型線圈背面附有鐵氧體隔磁片，阻斷 326kHz 磁場串入 Board B 地銅皮。
- [ ] **Type-C 倒角深度**：底蓋 6 點鐘 Type-C 凹槽寬度 12.0mm，確保車用粗線頭完全插入。
- [ ] **幾何干涉檢查**：前蓋 Ø42mm、底蓋 Ø42mm、手錶磁吸槽 Ø28.5mm，所有尺寸無懸空無穿模。
