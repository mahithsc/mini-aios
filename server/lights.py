from __future__ import annotations

import asyncio
import math
import board
from dataclasses import dataclass, field
import neopixel_spi as neopixel
import time
from typing import Any

Color = tuple[int, int, int]
Frame = list[Color]
LightMode = str


def scale_color(color: Color, level: float) -> Color:
    return tuple(max(0, min(255, int(channel * level))) for channel in color)


def blend_color(start: Color, end: Color, progress: float) -> Color:
    clamped = max(0.0, min(1.0, progress))
    return tuple(
        int(round(start[channel] + ((end[channel] - start[channel]) * clamped)))
        for channel in range(3)
    )


def ease_in_out(progress: float) -> float:
    clamped = max(0.0, min(1.0, progress))
    return clamped * clamped * (3.0 - (2.0 * clamped))


@dataclass(slots=True)
class LightsService:
    """Jetson LED controller with frame-based transitions between modes."""

    default_mode: LightMode = "idle"
    num_pixels: int = 61
    spi_frequency: int = 6_400_000
    max_brightness: float = 0.18
    transition_seconds: float = 0.35
    step_delay: float = 0.02
    idle_color: Color = (0, 255, 0)
    answering_min_level: float = 0.08
    answering_max_level: float = 1.0
    answering_breath_seconds: float = 2.8
    answering_color: Color = (120, 255, 80)
    thinking_step_delay: float = 0.04
    thinking_pulse_seconds: float = 2.0
    thinking_min_level: float = 0.05
    thinking_max_level: float = 1.0
    thinking_color: Color = (0, 255, 0)
    thinking_segment_offsets: tuple[int, ...] = (-1, 0, 1)
    thinking_segment_count: int = 4
    tool_color: Color = (80, 160, 255)
    error_color: Color = (255, 80, 80)
    off_color: Color = (0, 0, 0)
    _mode: LightMode | None = field(init=False, default=None)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _pixels: Any | None = field(init=False, default=None)
    _animation_task: asyncio.Task[None] | None = field(init=False, default=None)
    _hardware_available: bool = field(init=False, default=True)
    _current_frame: Frame = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._current_frame = self._solid_frame(self.off_color)

    @property
    def mode(self) -> LightMode:
        return self._mode or self.default_mode

    async def start(self) -> None:
        await self.set_mode(self.default_mode)

    async def shutdown(self) -> None:
        await self.set_mode("off")

    async def set_mode(self, mode: LightMode) -> None:
        async with self._lock:
            if mode == self._mode:
                return

            await self._stop_animation()
            self._mode = mode
            await self._apply_mode(mode)

    async def __call__(self, mode: LightMode) -> None:
        await self.set_mode(mode)

    async def _apply_mode(self, mode: LightMode) -> None:
        target_frame = self._frame_for_mode(mode, elapsed=0.0)
        await self._transition_to_frame(target_frame)

        if self._is_animated_mode(mode):
            self._animation_task = asyncio.create_task(self._run_mode_animation(mode))

    async def _run_mode_animation(self, mode: LightMode) -> None:
        start = time.monotonic()
        while True:
            if self._mode != mode:
                return

            elapsed = time.monotonic() - start
            self._render_frame(self._frame_for_mode(mode, elapsed=elapsed))
            await asyncio.sleep(self._animation_step_delay(mode))

    async def _stop_animation(self) -> None:
        if self._animation_task is None:
            return

        self._animation_task.cancel()
        try:
            await self._animation_task
        except asyncio.CancelledError:
            pass
        finally:
            self._animation_task = None

    async def _transition_to_frame(self, target_frame: Frame) -> None:
        start_frame = list(self._current_frame)
        if start_frame == target_frame:
            self._render_frame(target_frame)
            return

        steps = max(1, int(self.transition_seconds / max(self.step_delay, 0.001)))
        for step in range(1, steps + 1):
            progress = ease_in_out(step / steps)
            blended_frame = [
                blend_color(start_color, end_color, progress)
                for start_color, end_color in zip(start_frame, target_frame)
            ]
            self._render_frame(blended_frame)
            await asyncio.sleep(self.step_delay)

    def _is_animated_mode(self, mode: LightMode) -> bool:
        return mode in {"thinking", "answering"}

    def _animation_step_delay(self, mode: LightMode) -> float:
        if mode == "thinking":
            return self.thinking_step_delay
        return self.step_delay

    def _frame_for_mode(self, mode: LightMode, *, elapsed: float) -> Frame:
        if mode == "answering":
            phase = (elapsed % self.answering_breath_seconds) / self.answering_breath_seconds
            glow = 0.5 - 0.5 * math.cos(phase * 2.0 * math.pi)
            level = self.answering_min_level + (
                (self.answering_max_level - self.answering_min_level) * glow
            )
            return self._solid_frame(scale_color(self.answering_color, level))

        if mode == "thinking":
            pulse_phase = (elapsed % self.thinking_pulse_seconds) / self.thinking_pulse_seconds
            glow = 0.5 - 0.5 * math.cos(pulse_phase * 2.0 * math.pi)
            level = self.thinking_min_level + (
                (self.thinking_max_level - self.thinking_min_level) * glow
            )
            current_color = scale_color(self.thinking_color, level)
            position = int(elapsed / max(self.thinking_step_delay, 0.001)) % self.num_pixels
            frame = self._solid_frame(self.off_color)
            for offset in self._thinking_quarter_offsets():
                center = (position + offset) % self.num_pixels
                for segment_offset in self.thinking_segment_offsets:
                    frame[(center + segment_offset) % self.num_pixels] = current_color
            return frame

        if mode == "tool":
            return self._solid_frame(self.tool_color)

        if mode == "error":
            return self._solid_frame(self.error_color)

        if mode == "idle":
            return self._solid_frame(self.idle_color)

        return self._solid_frame(self.off_color)

    def _solid_frame(self, color: Color) -> Frame:
        return [color for _ in range(self.num_pixels)]

    def _thinking_quarter_offsets(self) -> tuple[int, ...]:
        if self.thinking_segment_count <= 1:
            return (0,)
        return tuple(
            int(round(index * self.num_pixels / self.thinking_segment_count))
            for index in range(self.thinking_segment_count)
        )

    def _ensure_pixels(self) -> bool:
        if not self._hardware_available:
            return False

        if self._pixels is not None:
            return True

        if board is None or neopixel is None:
            self._hardware_available = False
            return False

        try:
            self._pixels = neopixel.NeoPixel_SPI(
                board.SPI(),
                self.num_pixels,
                pixel_order=neopixel.GRB,
                frequency=self.spi_frequency,
                auto_write=False,
            )
            self._pixels.brightness = self.max_brightness
        except Exception:
            self._hardware_available = False
            self._pixels = None
            return False

        return True

    def _render_frame(self, frame: Frame) -> None:
        self._current_frame = list(frame)
        if not self._ensure_pixels():
            return

        for index, color in enumerate(frame):
            self._pixels[index] = color
        self._pixels.show()


lights = LightsService()
