# Backward-compatibility shim.
# All wake detection logic has moved to wake_detector.py.
# Existing code importing WakeSignalFusion continues to work unchanged.
from wake_detector import WakeDetector as WakeSignalFusion

__all__ = ["WakeSignalFusion"]
