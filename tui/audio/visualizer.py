"""
ASCII audio visualizer.
"""

import random
from typing import List


# Unicode block characters for bars
BARS = " ▁▂▃▄▅▆▇█"


class ASCIIVisualizer:
    """ASCII spectrum visualizer."""

    def __init__(self, width: int = 32, height: int = 1):
        self.width = width
        self.height = height
        self._smoothed_levels: List[float] = [0.0] * width
        self._smooth_factor = 0.3

    def render(self, levels: List[float]) -> str:
        """
        Render audio levels as ASCII bars.

        Args:
            levels: List of float values 0.0-1.0 representing audio levels

        Returns:
            String with ASCII bar visualization
        """
        # Ensure we have enough levels
        if len(levels) < self.width:
            # Pad with random low values for visual effect
            levels = levels + [random.random() * 0.3 for _ in range(self.width - len(levels))]

        # Take first N levels
        levels = levels[:self.width]

        # Smooth the levels for better visual effect
        for i, level in enumerate(levels):
            self._smoothed_levels[i] = (
                self._smoothed_levels[i] * self._smooth_factor +
                level * (1 - self._smooth_factor)
            )

        # Convert to bar characters
        bars = []
        for level in self._smoothed_levels:
            # Clamp to 0-1
            level = max(0.0, min(1.0, level))
            # Map to bar index (0-8)
            bar_idx = int(level * 8)
            bars.append(BARS[bar_idx])

        return "".join(bars)

    def render_multiline(self, levels: List[float]) -> str:
        """
        Render audio levels as multi-line ASCII bars.

        Args:
            levels: List of float values 0.0-1.0

        Returns:
            Multi-line string with vertical bars
        """
        if len(levels) < self.width:
            levels = levels + [random.random() * 0.3 for _ in range(self.width - len(levels))]

        levels = levels[:self.width]

        # Smooth levels
        for i, level in enumerate(levels):
            self._smoothed_levels[i] = (
                self._smoothed_levels[i] * self._smooth_factor +
                level * (1 - self._smooth_factor)
            )

        lines = []
        for row in range(self.height - 1, -1, -1):
            threshold = row / self.height
            line = ""
            for level in self._smoothed_levels:
                if level > threshold:
                    line += "█"
                else:
                    line += " "
            lines.append(line)

        return "\n".join(lines)

    def reset(self):
        """Reset smoothed levels."""
        self._smoothed_levels = [0.0] * self.width


class WaveformVisualizer:
    """ASCII waveform visualizer."""

    WAVE_CHARS = "▁▂▃▄▅▆▇█▇▆▅▄▃▂▁"

    def __init__(self, width: int = 32):
        self.width = width
        self._phase = 0.0

    def render(self, intensity: float = 0.5) -> str:
        """
        Render animated waveform.

        Args:
            intensity: Overall intensity 0.0-1.0

        Returns:
            String with waveform visualization
        """
        import math

        chars = []
        for i in range(self.width):
            # Create wave pattern
            wave = math.sin((i / self.width * 4 + self._phase) * math.pi)
            wave = (wave + 1) / 2  # Normalize to 0-1
            wave *= intensity

            # Add some noise
            wave += random.random() * 0.2 * intensity
            wave = max(0, min(1, wave))

            # Map to character
            char_idx = int(wave * 8)
            chars.append(BARS[char_idx])

        # Advance phase
        self._phase += 0.2

        return "".join(chars)
