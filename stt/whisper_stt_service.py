import os
import uuid
import time
import logging
import datetime
import numpy as np
from typing import AsyncGenerator, Optional, Dict, Any, Mapping

import lightning_whisper_mlx
from stt.light_whisper_streaming import OnlineSTTProcessor, KoreanTokenizer

from pipecat.services.stt_service import STTService
from pipecat.frames.frames import Frame, AudioRawFrame, TranscriptionFrame, InterimTranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.time import time_now_iso8601

logger = logging.getLogger(__name__)


class WhisperSTTService(STTService):
	"""
	Whisper 모델을 사용하는 STT 서비스
	"""
	def __init__(
		self,
		model_name="small",
		buffer_seconds=15.0,
		buffer_trimming="segment",
		audio_passthrough=False,
		sample_rate: Optional[int] = 16000,
		min_chunk_size: float = 0.5,  # 최소 처리 단위 (초)
		**kwargs,
	):
		super().__init__(
			audio_passthrough=audio_passthrough,
			sample_rate=sample_rate,
			**kwargs
		)
		self._model_name = model_name
		self.buffer_trimming = buffer_trimming
		self.model = None
		self.stt_processor = None
		self.user_id = f"whisper-{uuid.uuid4()}"
		self.buffer_seconds = buffer_seconds
		self.min_chunk_size = min_chunk_size
		
		# 오디오 버퍼링을 위한 변수들
		self.audio_buffer = []
		self.buffer_samples = 0
		self.target_samples = int(min_chunk_size * (sample_rate or 16000))
		
		self._settings = {
			"model": model_name,
			"language": "ko"
		}
		self.init_model()
	
	@property
	def model_name(self):
		"""모델 이름 getter"""
		return self._model_name
		
	def init_model(self):
		"""Whisper 모델 초기화"""
		logger.info(f"Lightning Whisper MLX 모델 로드 중 ({self._model_name})...")
		start_time = time.time()
		self.model = lightning_whisper_mlx.LightningWhisperMLX(model=self._model_name, batch_size=12)
		
		self.stt_processor = OnlineSTTProcessor(
			lightning_whisper=self.model,
			buffer_seconds=self.buffer_seconds,
			tokenizer=KoreanTokenizer(),
		)
		self.stt_processor.init()
		
		# 오디오 버퍼 초기화
		self.audio_buffer = []
		self.buffer_samples = 0
	
	async def set_model(self, model: str):
		"""모델 변경 시 호출"""
		await super().set_model(model)
		self._model_name = model
		self.init_model()
	
	async def set_language(self, language):
		"""언어 설정 변경 시 호출"""
		pass
	
	async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
		# 오디오를 float32로 변환
		pcm_f32 = (np.frombuffer(audio, np.int16)
				.astype(np.float32, copy=False) / 32768.0)
		
		current_time = time.time()
		
		final_res = self.stt_processor.insert_audio_chunk(pcm_f32, current_time)
		if isinstance(final_res, dict):
			text = final_res["text"]
			if text:
				timestamp = str(datetime.datetime.now().timestamp())
				self
				yield TranscriptionFrame(
					text=text,
					language="ko-KR",
					user_id=self.user_id,
					timestamp=timestamp
				)
			return
		
		
		start, end, interim_text, meta = self.stt_processor.process_iter()
		if interim_text:
			logger.debug(f"WhisperSTT 임시 인식: {interim_text}")
			timestamp = str(datetime.datetime.now().timestamp())
			yield InterimTranscriptionFrame(
				text=interim_text,
				language="ko-KR",
				user_id=self.user_id,
				timestamp=timestamp
			)
	
	async def start(self, frame):
		await super().start(frame)
		logger.info(f"WhisperSTT 서비스 시작 - 샘플레이트: {self.sample_rate}")
		self.audio_buffer = []
		self.buffer_samples = 0
	
	async def stop(self, frame):
		"""서비스 종료"""
		await super().stop(frame)
		
		# 남은 버퍼 처리
		if self.audio_buffer and self.buffer_samples > 0:
			combined_audio = np.concatenate(self.audio_buffer)
			result = self.stt_processor.insert_audio_chunk(combined_audio, time.time())
			
			if isinstance(result, dict) and result.get("text"):
				timestamp = str(datetime.datetime.now().timestamp())
				# 마지막 결과는 yield할 수 없으므로 로깅만
				logger.info(f"WhisperSTT 종료 시 최종 인식: {result['text']}")
		
		logger.info("WhisperSTT 서비스 종료")
	
	def can_generate_metrics(self) -> bool:
		"""메트릭스 생성 여부"""
		return True