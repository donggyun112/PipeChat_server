import time
from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame,
    UserStoppedSpeakingFrame,
    EmulateUserStoppedSpeakingFrame,
    TranscriptionFrame,
    MetricsFrame,
    TransportMessageUrgentFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TransportMessageUrgentFrame,
)
from pipecat.metrics.metrics import MetricsData

class TranscriptionTimingLogger(FrameProcessor):
    """
    TranscriptionFrame TTFB 측정기
    """
    def __init__(self, rtvi_processor=None):
        super().__init__(name="TranscriptionTimingLogger")
        self.rtvi_processor = rtvi_processor
        self.last_stop_ts: float = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (UserStoppedSpeakingFrame, EmulateUserStoppedSpeakingFrame)):
            self.last_stop_ts = time.time()

        elif isinstance(frame, TranscriptionFrame) and self.last_stop_ts > 0:
            elapsed = time.time() - self.last_stop_ts
            logger.info(f"{self.name}: TTFB {elapsed:.3f}s | 텍스트: '{frame.text}'")

            metrics = MetricsData(
                name="time_to_final_transcription",
                value=elapsed,
                tags={"metric": "ttfb"},
                processor=self.name
            )
            
            metrics_frame = MetricsFrame(data=metrics)
            await self.push_frame(metrics_frame, direction)
            
            # RTVI 프로세서가 있으면 직접 메트릭 전송 - 프레임 생성 후 프로세서로 전달
            if self.rtvi_processor:
                # 메트릭 메시지 생성
                metrics_message = {
                    "label": "rtvi-ai",
                    "type": "metrics",
                    "data": {
                        "service": "stt",
                        "ttfb_ms": int(elapsed * 1000),
                        "timestamp": int(time.time() * 1000)
                    }
                }
                
                transport_frame = TransportMessageUrgentFrame(message=metrics_message)
                await self.rtvi_processor.push_frame(transport_frame)
            self.last_stop_ts = 0.0
            
        await self.push_frame(frame, direction)