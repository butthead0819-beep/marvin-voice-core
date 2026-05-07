import davey
import sys
import logging
import select
import socket
import errno
import time
import os
from types import ModuleType
import discord
from discord.ext import voice_recv

logger = logging.getLogger(__name__)
DEBUG_UDP_HEARTBEAT = os.getenv("DEBUG_UDP_HEARTBEAT", "false").lower() == "true"

# 🚀 DAVE Protocol Compatibility Layer (v0.1.5 Fix)
def apply_davey_fix():
    print("🛠️  Applying DAVE 0.1.5 compatibility fix...")
    
    # 1. 建立偽裝的 mls 模組
    mls = ModuleType('davey.mls')
    
    # 2. 映射 0.1.5 版本的類別到 Pycord 期待的名稱
    if hasattr(davey, 'DaveSession'):
        mls.MLSContext = davey.DaveSession
        print("✅ Mapped davey.DaveSession to davey.mls.MLSContext")
    
    # 3. 注入到系統模組中
    sys.modules['davey.mls'] = mls
    davey.mls = mls 
    
    print("🎉 DAVE Compatibility Layer injected successfully!")

def apply_macos_udp_patch():
    """
    [Hotfix] 針對 macOS Errno 56 (EISCONN) 的動態修補與底層封包監控。
    """
    logger.info("🔧 [DaveyBridge] 正在注入 macOS UDP 網路層修補程式...")

    # ==========================================
    # 補丁 1: 強力型 SocketReader (取代原生迴圈，提供持續監控)
    # ==========================================
    def aggressive_do_run(self):
        print("🧤 [Patch] Aggressive Socket Watchdog Starting...", flush=True)
        self._end.clear()
        self._running.set()
        
        raw_packet_total = 0
        while not self._end.is_set():
            if not self._running.is_set():
                self._running.wait()
                continue

            try:
                # 取得底層 Socket
                sock = getattr(self.state, 'socket', None)
                if not sock or not isinstance(sock, socket.socket):
                    time.sleep(0.5)
                    continue

                # 監聽 Socket 是否可讀
                ready, _, _ = select.select([sock], [], [], 1.0)
                if not ready:
                    continue

                # 讀取數據 (UDP 封包上限通常為 1500-2048)
                data = sock.recv(2048)
                raw_packet_total += 1
                
                # 每 500 個封包印一次心跳，確保網絡層沒死
                if DEBUG_UDP_HEARTBEAT and (raw_packet_total <= 5 or raw_packet_total % 500 == 0):
                    print(f"📡 [UDP Heartbeat] Received raw packet (Count: {raw_packet_total}, Size: {len(data)})", flush=True)
                
                # 將封包派發給所有回呼 (包括 voice_recv 的 AudioReader)
                for cb in list(self._callbacks):
                    try:
                        cb(data)
                    except Exception as e:
                        # 避免單個回呼崩潰拖垮整個讀取迴圈
                        pass

            except OSError as e:
                # 處理 macOS 特有的 EISCONN 或其他網絡抖動
                if e.errno == 56: # EISCONN
                    # 如果 Socket 已經 connected，通常 recv 應該沒問題，但如果出錯則跳過
                    continue
                time.sleep(0.1)
            except Exception as e:
                print(f"❌ [Aggressive Reader] Unexpected Error: {e}")
                time.sleep(1)

        print("🛑 [Aggressive Reader] Watchdog Stopping.", flush=True)

    discord.voice_state.SocketReader._do_run = aggressive_do_run
    print("✅ Applied monkey-patch: voice_state.SocketReader._do_run (Aggressive)")

    # ==========================================
    # 補丁 2: 攔截 voice_recv 的 AudioReader 並監聽 Raw Packets
    # ==========================================
    if hasattr(voice_recv.reader, 'AudioReader'):
        original_callback = voice_recv.reader.AudioReader.callback
        packet_log_counter = 0

        def patched_callback(self, packet_data: bytes):
            nonlocal packet_log_counter
            
            if packet_log_counter < 10:
                print(f"📥 [AudioReader] Incoming packet to STT Sink (Size: {len(packet_data)})", flush=True)
                packet_log_counter += 1
            elif packet_log_counter == 10:
                print("📥 [AudioReader] STT 数据流管道正常，隱藏後續大量日誌。", flush=True)
                packet_log_counter += 1

            return original_callback(self, packet_data)

        voice_recv.reader.AudioReader.callback = patched_callback
        print("✅ Applied monkey-patch: voice_recv.reader.AudioReader.callback")

    # ==========================================
    # 補丁 3: 攔截 UDPKeepAlive 防止 Errno 56 造成的死循環
    # ==========================================
    try:
        from discord.ext.voice_recv.reader import UDPKeepAlive
        
        def patched_keepalive_run(self):
            self.voice_client.wait_until_connected()
            while not self._end_thread.is_set():
                vc = self.voice_client
                try:
                    packet = self.counter.to_bytes(8, 'big')
                    # Discord 語音 Keepalive 通常發送到 endpoint_ip:voice_port
                    try:
                        vc._connection.socket.sendto(packet, (vc._connection.endpoint_ip, vc._connection.voice_port))
                    except OSError as e:
                        if e.errno == 56: # EISCONN
                            vc._connection.socket.send(packet)
                        else: raise
                except Exception:
                    time.sleep(self.delay)
                    if not vc.is_connected(): break
                else:
                    self.counter += 1
                    time.sleep(self.delay)
                    
        UDPKeepAlive.run = patched_keepalive_run
        print("✅ Applied monkey-patch: UDPKeepAlive.run")
    except Exception as e:
        print(f"⚠️  UDPKeepAlive Patch Failed: {e}")

    print("✅ [DaveyBridge] macOS Network Patches Applied Successfully.")

if __name__ == "__main__":
    apply_davey_fix()
    apply_macos_udp_patch()
