"""CryptoError log 抽樣：discord.ext.voice_recv.reader 每天 ~3800 行 CryptoError ERROR
淹沒真 error。抽樣 1/N 放行（附累計數），其餘 drop；非 CryptoError 記錄一律放行。
"""
import logging

from log_filters import CryptoErrorSampler


def _rec(msg: str, level=logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("discord.ext.voice_recv.reader", level, __file__, 1, msg, (), None)


def test_non_cryptoerror_always_passes():
    f = CryptoErrorSampler(sample_rate=100)
    assert f.filter(_rec("some other reader warning")) is True
    assert f.filter(_rec("rtcp packet parsed")) is True


def test_first_cryptoerror_passes_rest_dropped():
    f = CryptoErrorSampler(sample_rate=100)
    results = [f.filter(_rec("CryptoError decoding packet data")) for _ in range(100)]
    assert results[0] is True                 # 第 1 筆放行
    assert all(r is False for r in results[1:])   # 2..100 全 drop


def test_samples_every_n():
    f = CryptoErrorSampler(sample_rate=100)
    passed = sum(1 for _ in range(250) if f.filter(_rec("CryptoError decoding packet data")))
    assert passed == 3                        # 第 1 / 101 / 201 筆放行


def test_emitted_record_carries_cumulative_count():
    f = CryptoErrorSampler(sample_rate=10)
    rec = _rec("CryptoError decoding packet data")
    assert f.filter(rec) is True
    assert "CryptoError" in rec.getMessage()
    assert "累計" in rec.getMessage()          # 附累計數，仍看得到量


def test_non_cryptoerror_not_counted():
    # 夾雜的非 CryptoError 不該打亂抽樣計數
    f = CryptoErrorSampler(sample_rate=3)
    f.filter(_rec("unrelated"))
    r1 = f.filter(_rec("CryptoError x"))       # 第 1 筆 CryptoError
    f.filter(_rec("unrelated"))
    r2 = f.filter(_rec("CryptoError x"))       # 第 2 筆
    assert r1 is True and r2 is False
