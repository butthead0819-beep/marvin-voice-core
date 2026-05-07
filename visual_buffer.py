import collections
from typing import List, Tuple

class VisualBuffer:
    """
    A memory-efficient ring buffer for storing JPEG-compressed visual frames.
    Strictly limited to 600 frames to comply with M1 8GB RAM constraints.
    """
    def __init__(self, maxlen: int = 600):
        if maxlen > 600:
            # Enforce strict hardware limit as requested
            maxlen = 600
        self.maxlen = maxlen
        # Stores tuples of (timestamp, jpeg_bytes)
        self._buffer: collections.deque[Tuple[float, bytes]] = collections.deque(maxlen=maxlen)

    async def add_frame(self, timestamp: float, jpeg_bytes: bytes) -> None:
        """
        Adds a new frame to the buffer.
        Automatically evicts the oldest frame if the buffer is full.
        
        Args:
            timestamp: Precise epoch timestamp of the frame.
            jpeg_bytes: JPEG-compressed image data.
        """
        self._buffer.append((timestamp, jpeg_bytes))

    async def get_frames_around(self, wake_time: float, before: float, after: float) -> List[Tuple[float, bytes]]:
        """
        Retrieves frames within a specific time window relative to a wake event.
        
        Args:
            wake_time: The timestamp when the wake event (e.g., wake word) occurred.
            before: Seconds before wake_time to include.
            after: Seconds after wake_time to include.
            
        Returns:
            List[Tuple[float, bytes]]: A list of tuples containing (timestamp, jpeg_bytes).
            Ordered by timestamp (oldest to newest).
        """
        start_time = wake_time - before
        end_time = wake_time + after
        
        # Create a snapshot to prevent 'RuntimeError: deque mutated during iteration'
        buffer_snapshot = list(self._buffer)
        
        results = [
            frame for frame in buffer_snapshot
            if start_time <= frame[0] <= end_time
        ]
        return results

    async def clear(self) -> None:
        """Clears all frames from the buffer to free memory."""
        self._buffer.clear()

    @property
    def count(self) -> int:
        """Returns the current number of frames in the buffer."""
        return len(self._buffer)
