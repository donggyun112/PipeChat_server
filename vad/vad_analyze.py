import time
from typing import Optional
import numpy as np

from loguru import logger
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADState, VADParams
from vad.voice_check import VoiceDetector

class CustomVADAnalyzer(VADAnalyzer):
    """
    VoiceDetector를 사용하는 VADAnalyzer 구현
    - detect_voice 메서드만 활용하여 빠른 응답
    """
    def __init__(self, *, 
                 sample_rate: Optional[int] = 16000, 
                 params: VADParams = VADParams(),
                 energy_threshold: float = 60,
                 min_continuous_speech: float = 0.3,
                 speech_debounce_time: float = 0.5):
        super().__init__(sample_rate=sample_rate, params=params)
        
        # VoiceDetector 설정
        self.voice_detector = VoiceDetector(
            sample_rate=sample_rate or 16000,
            energy_threshold=energy_threshold,
            vad_threshold=params.confidence,
            silence_limit=params.stop_secs,
            speech_debounce_time=speech_debounce_time,
            min_continuous_speech=min_continuous_speech,
            use_event_manager=False,
            min_buffer_size=512
        )
        
        self.last_speech_prob = 0.0
        self._chunk_size = 512
        self._last_voice_detected = False
    
    def num_frames_required(self) -> int:
        return self._chunk_size
    
    def voice_confidence(self, buffer) -> float:
        try:
            is_voice_detected = self.voice_detector.detect_voice(buffer)
            self._last_voice_detected = is_voice_detected
            
            if self._last_voice_detected:
                self.last_speech_prob = 1.0
            else:
                self.last_speech_prob = 0.0
                
            return self.last_speech_prob
            
        except Exception as e:
            logger.error(f"Error in detect_voice: {e}")
            return 0
    
    def _get_smoothed_volume(self, audio: bytes) -> float:
        # 현재 에너지 레벨 사용
        if hasattr(self.voice_detector, 'current_energy'):
            return self.voice_detector.current_energy / 100.0  # 정규화
        else:
            # 에너지 레벨이 없으면 부모 메서드 사용
            return super()._get_smoothed_volume(audio)
    
    def set_sample_rate(self, sample_rate: int):
        """샘플 레이트 설정"""
        super().set_sample_rate(sample_rate)
        if hasattr(self.voice_detector, 'sample_rate'):
            self.voice_detector.sample_rate = sample_rate
    
    def set_params(self, params: VADParams):
        super().set_params(params)
        
        if hasattr(self.voice_detector, 'vad_threshold'):
            self.voice_detector.vad_threshold = params.confidence
        if hasattr(self.voice_detector, 'silence_limit'):
            self.voice_detector.silence_limit = params.stop_secs
    
    def get_is_speaking(self) -> bool:
        return self._last_voice_detected