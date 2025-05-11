from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from loguru import logger

class FrameTap(FrameProcessor):
    def __init__(self, name="Tap"):
        super().__init__(name=name)
        self.logger = logger.bind(name=name)

    async def process_frame(self, frame, direction: FrameDirection):
        self.logger.debug(f"Processing frame: {frame} in direction: {direction}")
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)